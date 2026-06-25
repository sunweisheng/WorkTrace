from __future__ import annotations

from dataclasses import dataclass

from ..config import RuntimeConfig
from ..models import ConversationSlice, NormalizedMessage


@dataclass(frozen=True)
class AnchorCluster:
    conversation_id: str
    message_ids: list[str]


def group_anchor_clusters(
    messages: list[NormalizedMessage],
    self_open_id: str,
) -> list[AnchorCluster]:
    clusters: list[AnchorCluster] = []
    current: list[str] = []
    current_conversation_id: str | None = None
    previous_was_self = False

    for message in messages:
        is_self = message.sender_open_id == self_open_id
        if not is_self:
            previous_was_self = False
            continue

        if (
            previous_was_self
            and current
            and current_conversation_id == message.conversation_id
        ):
            current.append(message.message_id)
        else:
            if current and current_conversation_id is not None:
                clusters.append(
                    AnchorCluster(
                        conversation_id=current_conversation_id,
                        message_ids=current,
                    )
                )
            current = [message.message_id]
            current_conversation_id = message.conversation_id

        previous_was_self = True

    if current and current_conversation_id is not None:
        clusters.append(
            AnchorCluster(conversation_id=current_conversation_id, message_ids=current)
        )
    return clusters


def build_base_window(
    messages: list[NormalizedMessage],
    cluster: AnchorCluster,
    before_limit: int,
    after_limit: int,
) -> set[str]:
    index_by_id = {message.message_id: index for index, message in enumerate(messages)}
    anchor_indexes = [index_by_id[mid] for mid in cluster.message_ids if mid in index_by_id]
    if not anchor_indexes:
        return set()

    first_anchor = min(anchor_indexes)
    last_anchor = max(anchor_indexes)

    before_indexes = list(range(max(0, first_anchor - before_limit), first_anchor))
    after_indexes = list(range(last_anchor + 1, min(len(messages), last_anchor + 1 + after_limit)))
    window_indexes = before_indexes + anchor_indexes + after_indexes
    return {messages[index].message_id for index in window_indexes}


def expand_direct_relations(
    messages: list[NormalizedMessage],
    cluster: AnchorCluster,
    window_ids: set[str],
) -> set[str]:
    anchor_ids = set(cluster.message_ids)
    expanded = set(window_ids)

    for message in messages:
        related = {
            message.reply_to_message_id,
            message.quote_message_id,
        }
        if message.message_id in anchor_ids:
            expanded.update(filter(None, related))
            continue

        if any(target in anchor_ids for target in related if target):
            expanded.add(message.message_id)
            expanded.update(filter(None, related))

        if message.message_id in expanded and any(target in anchor_ids for target in related if target):
            expanded.add(message.message_id)

    return expanded


def merge_overlapping_slices(candidate_slices: list[dict[str, object]]) -> list[dict[str, object]]:
    if not candidate_slices:
        return []

    remaining = sorted(
        candidate_slices,
        key=lambda item: (
            str(item["conversation_id"]),
            min(item["indexes"]) if item["indexes"] else 0,
        ),
    )
    merged: list[dict[str, object]] = []

    for candidate in remaining:
        candidate_ids = set(candidate["message_ids"])
        candidate_anchors = set(candidate["anchor_message_ids"])
        candidate_indexes = set(candidate["indexes"])
        connected = False

        for existing in merged:
            if existing["conversation_id"] != candidate["conversation_id"]:
                continue

            overlap = bool(set(existing["message_ids"]) & candidate_ids)
            contains_anchor = bool(
                set(existing["message_ids"]) & candidate_anchors
                or candidate_ids & set(existing["anchor_message_ids"])
            )
            related = bool(set(existing["indexes"]) & candidate_indexes)
            if overlap or contains_anchor or related:
                existing["message_ids"] = sorted(
                    set(existing["message_ids"]) | candidate_ids
                )
                existing["anchor_message_ids"] = sorted(
                    set(existing["anchor_message_ids"]) | candidate_anchors
                )
                existing["indexes"] = sorted(set(existing["indexes"]) | candidate_indexes)
                connected = True
                break

        if not connected:
            merged.append(
                {
                    "conversation_id": candidate["conversation_id"],
                    "conversation_name": candidate["conversation_name"],
                    "message_ids": sorted(candidate_ids),
                    "anchor_message_ids": sorted(candidate_anchors),
                    "indexes": sorted(candidate_indexes),
                }
            )

    return merged


def trim_slice_by_priority(
    messages: list[NormalizedMessage],
    message_ids: set[str],
    anchor_ids: set[str],
    max_base_limit: int,
) -> list[NormalizedMessage]:
    selected = [message for message in messages if message.message_id in message_ids]
    if len(selected) <= max_base_limit:
        return selected

    anchor_set = set(anchor_ids)
    relation_ids = {
        message.message_id
        for message in selected
        if (message.reply_to_message_id in anchor_set or message.quote_message_id in anchor_set)
    }

    def priority(message: NormalizedMessage) -> tuple[int, str, str]:
        if message.message_id in anchor_set:
            return (0, message.send_time, message.message_id)
        if message.message_id in relation_ids:
            return (1, message.send_time, message.message_id)
        return (2, message.send_time, message.message_id)

    chosen = sorted(selected, key=priority)[:max_base_limit]
    chosen_ids = {message.message_id for message in chosen}
    return [message for message in selected if message.message_id in chosen_ids]


def build_conversation_slices(
    messages: list[NormalizedMessage],
    self_open_id: str,
    config: RuntimeConfig,
) -> list[ConversationSlice]:
    by_conversation: dict[str, list[NormalizedMessage]] = {}
    for message in messages:
        by_conversation.setdefault(message.conversation_id, []).append(message)

    candidate_slices: list[dict[str, object]] = []
    for conversation_id, conversation_messages in by_conversation.items():
        clusters = group_anchor_clusters(conversation_messages, self_open_id)
        for cluster in clusters:
            window_ids = build_base_window(
                conversation_messages,
                cluster,
                config.slice_context_before,
                config.slice_context_after,
            )
            expanded = expand_direct_relations(conversation_messages, cluster, window_ids)
            indexes = {
                index
                for index, message in enumerate(conversation_messages)
                if message.message_id in expanded
            }
            candidate_slices.append(
                {
                    "conversation_id": conversation_id,
                    "conversation_name": conversation_messages[0].conversation_name
                    if conversation_messages
                    else "",
                    "message_ids": sorted(expanded),
                    "anchor_message_ids": cluster.message_ids,
                    "indexes": sorted(indexes),
                }
            )

    merged = merge_overlapping_slices(candidate_slices)
    slices: list[ConversationSlice] = []
    for item in merged:
        conversation_messages = by_conversation[str(item["conversation_id"])]
        trimmed_messages = trim_slice_by_priority(
            conversation_messages,
            set(item["message_ids"]),
            set(item["anchor_message_ids"]),
            config.slice_base_limit,
        )
        anchor_ids = [
            message_id
            for message_id in item["anchor_message_ids"]
            if any(msg.message_id == message_id for msg in trimmed_messages)
        ]
        ordered_ids = [message.message_id for message in trimmed_messages]
        slice_id = build_slice_id(
            conversation_id=str(item["conversation_id"]),
            anchor_message_ids=anchor_ids,
        )
        slices.append(
            ConversationSlice(
                slice_id=slice_id,
                conversation_id=str(item["conversation_id"]),
                conversation_name=str(item["conversation_name"]),
                anchor_message_ids=anchor_ids,
                in_day_message_ids=ordered_ids,
                messages=trimmed_messages,
                attachment_texts=[],
            )
        )

    return sorted(slices, key=lambda item: (item.conversation_id, item.slice_id))


def build_slice_id(conversation_id: str, anchor_message_ids: list[str]) -> str:
    suffix = "-".join(anchor_message_ids[:3]) if anchor_message_ids else "empty"
    return f"{conversation_id}:{suffix}"
