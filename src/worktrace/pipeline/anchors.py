from __future__ import annotations

from dataclasses import dataclass

from ..config import RuntimeConfig
from ..models import AnchorUnit, AttachmentMeta, NormalizedMessage


@dataclass(frozen=True)
class RawAnchorCluster:
    conversation_id: str
    message_ids: list[str]


def group_anchor_units(
    messages: list[NormalizedMessage],
    self_open_id: str,
    config: RuntimeConfig,
) -> list[AnchorUnit]:
    by_conversation: dict[str, list[NormalizedMessage]] = {}
    for message in messages:
        by_conversation.setdefault(message.conversation_id, []).append(message)

    anchor_units: list[AnchorUnit] = []
    for conversation_id, conversation_messages in by_conversation.items():
        clusters = _group_raw_anchor_clusters(conversation_messages, self_open_id)
        for cluster in clusters:
            base_message_ids = build_anchor_base_window(
                conversation_messages,
                cluster,
                config.slice_context_before,
                config.slice_context_after,
            )
            expanded_message_ids = expand_anchor_direct_relations(
                conversation_messages,
                cluster,
                base_message_ids,
            )
            selected_messages = [
                item for item in conversation_messages if item.message_id in expanded_message_ids
            ]
            reply_relation_ids = sorted(
                {
                    item.reply_to_message_id
                    for item in selected_messages
                    if item.reply_to_message_id and item.reply_to_message_id in expanded_message_ids
                }
            )
            quote_relation_ids = sorted(
                {
                    item.quote_message_id
                    for item in selected_messages
                    if item.quote_message_id and item.quote_message_id in expanded_message_ids
                }
            )
            attachment_refs = _collect_attachment_refs(selected_messages)
            anchor_units.append(
                AnchorUnit(
                    anchor_unit_id=_build_anchor_unit_id(conversation_id, cluster.message_ids),
                    conversation_id=conversation_id,
                    conversation_name=conversation_messages[0].conversation_name
                    if conversation_messages
                    else "",
                    anchor_message_ids=list(cluster.message_ids),
                    in_day_message_ids=[item.message_id for item in selected_messages],
                    base_message_ids=sorted(base_message_ids),
                    messages=selected_messages,
                    reply_relation_ids=reply_relation_ids,
                    quote_relation_ids=quote_relation_ids,
                    attachment_refs=attachment_refs,
                )
            )
    return anchor_units


def build_anchor_base_window(
    messages: list[NormalizedMessage],
    cluster: RawAnchorCluster,
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


def expand_anchor_direct_relations(
    messages: list[NormalizedMessage],
    cluster: RawAnchorCluster,
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

    return expanded


def _group_raw_anchor_clusters(
    messages: list[NormalizedMessage],
    self_open_id: str,
) -> list[RawAnchorCluster]:
    clusters: list[RawAnchorCluster] = []
    current: list[str] = []
    previous_was_self = False

    for message in messages:
        is_self = message.sender_open_id == self_open_id
        if not is_self:
            previous_was_self = False
            continue

        if previous_was_self and current:
            current.append(message.message_id)
        else:
            if current:
                clusters.append(
                    RawAnchorCluster(
                        conversation_id=message.conversation_id,
                        message_ids=current,
                    )
                )
            current = [message.message_id]

        previous_was_self = True

    if current:
        clusters.append(
            RawAnchorCluster(
                conversation_id=messages[0].conversation_id if messages else "",
                message_ids=current,
            )
        )
    return clusters


def _build_anchor_unit_id(conversation_id: str, anchor_message_ids: list[str]) -> str:
    return f"{conversation_id}:{'-'.join(anchor_message_ids)}"


def _collect_attachment_refs(messages: list[NormalizedMessage]) -> list[AttachmentMeta]:
    seen: set[str] = set()
    attachments: list[AttachmentMeta] = []
    for message in messages:
        for attachment in message.attachments:
            if attachment.attachment_id in seen:
                continue
            seen.add(attachment.attachment_id)
            attachments.append(attachment)
    return attachments
