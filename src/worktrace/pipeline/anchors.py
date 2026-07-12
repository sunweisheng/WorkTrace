from __future__ import annotations

from dataclasses import dataclass, replace

from ..models import AnchorSignal, AnchorUnit, AttachmentMeta, NormalizedMessage
from ..reaction_catalog import ReactionCatalog


@dataclass(frozen=True)
class RawAnchorCluster:
    conversation_id: str
    message_ids: list[str]


def group_anchor_units(
    messages: list[NormalizedMessage],
    self_open_id: str,
    *,
    before_limit: int,
    after_limit: int,
    reaction_catalog: ReactionCatalog | None = None,
) -> list[AnchorUnit]:
    by_conversation: dict[str, list[NormalizedMessage]] = {}
    for message in messages:
        by_conversation.setdefault(message.conversation_id, []).append(message)

    catalog = reaction_catalog or ReactionCatalog.empty("")
    anchor_units: list[AnchorUnit] = []
    for conversation_id, conversation_messages in by_conversation.items():
        text_units = _build_text_anchor_units(
            conversation_messages,
            self_open_id=self_open_id,
            before_limit=before_limit,
            after_limit=after_limit,
        )
        reaction_signals = _build_reaction_signals(
            conversation_messages,
            self_open_id=self_open_id,
            reaction_catalog=catalog,
        )

        attached_signals: dict[str, list[AnchorSignal]] = {
            unit.anchor_unit_id: list(unit.anchor_signals) for unit in text_units
        }
        reaction_units: list[AnchorUnit] = []
        for signal in reaction_signals:
            containing_unit = next(
                (
                    unit
                    for unit in text_units
                    if signal.message_id in set(unit.in_day_message_ids)
                ),
                None,
            )
            if containing_unit is not None:
                attached_signals[containing_unit.anchor_unit_id].append(signal)
                continue
            reaction_units.append(
                _build_anchor_unit(
                    conversation_messages,
                    RawAnchorCluster(conversation_id, [signal.message_id]),
                    before_limit=before_limit,
                    after_limit=after_limit,
                    anchor_signals=[signal],
                    unit_suffix=signal.signal_id,
                )
            )

        text_units = [
            replace(
                unit,
                anchor_signals=sorted(
                    attached_signals[unit.anchor_unit_id],
                    key=lambda item: (item.action_time, item.signal_id),
                ),
            )
            for unit in text_units
        ]
        all_units = text_units + reaction_units
        index_by_message_id = {
            message.message_id: index for index, message in enumerate(conversation_messages)
        }
        anchor_units.extend(
            sorted(
                all_units,
                key=lambda unit: (
                    min(index_by_message_id[item] for item in unit.anchor_message_ids),
                    unit.anchor_unit_id,
                ),
            )
        )
    return anchor_units


def _build_text_anchor_units(
    messages: list[NormalizedMessage],
    *,
    self_open_id: str,
    before_limit: int,
    after_limit: int,
) -> list[AnchorUnit]:
    units: list[AnchorUnit] = []
    for cluster in _group_raw_anchor_clusters(messages, self_open_id):
        signals = [
            AnchorSignal(
                signal_id=f"text:{message_id}",
                kind="text",
                message_id=message_id,
                action_time=next(
                    item.send_time for item in messages if item.message_id == message_id
                ),
            )
            for message_id in cluster.message_ids
        ]
        units.append(
            _build_anchor_unit(
                messages,
                cluster,
                before_limit=before_limit,
                after_limit=after_limit,
                anchor_signals=signals,
            )
        )
    return units


def _build_reaction_signals(
    messages: list[NormalizedMessage],
    *,
    self_open_id: str,
    reaction_catalog: ReactionCatalog,
) -> list[AnchorSignal]:
    signals: list[AnchorSignal] = []
    seen: set[str] = set()
    for message in messages:
        for index, reaction in enumerate(message.reactions):
            if reaction.operator_open_id != self_open_id:
                continue
            signal_id = reaction.reaction_id or f"reaction:{message.message_id}:{index}"
            if signal_id in seen:
                continue
            seen.add(signal_id)
            metadata = reaction_catalog.lookup(reaction.emoji_type)
            signals.append(
                AnchorSignal(
                    signal_id=signal_id,
                    kind="reaction",
                    message_id=message.message_id,
                    action_time=reaction.action_time or message.send_time,
                    emoji_type=reaction.emoji_type,
                    emoji_name=metadata.name,
                    emoji_description=metadata.description,
                    semantic=metadata.semantic,
                )
            )
    return sorted(signals, key=lambda item: (item.action_time, item.signal_id))


def _build_anchor_unit(
    messages: list[NormalizedMessage],
    cluster: RawAnchorCluster,
    *,
    before_limit: int,
    after_limit: int,
    anchor_signals: list[AnchorSignal],
    unit_suffix: str = "",
) -> AnchorUnit:
    base_message_ids = build_anchor_base_window(
        messages,
        cluster,
        before_limit,
        after_limit,
    )
    expanded_message_ids = expand_anchor_direct_relations(messages, cluster, base_message_ids)
    selected_messages = [item for item in messages if item.message_id in expanded_message_ids]
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
    anchor_unit_id = _build_anchor_unit_id(cluster.conversation_id, cluster.message_ids)
    if unit_suffix:
        anchor_unit_id = f"{anchor_unit_id}:{unit_suffix}"
    return AnchorUnit(
        anchor_unit_id=anchor_unit_id,
        conversation_id=cluster.conversation_id,
        conversation_name=messages[0].conversation_name if messages else "",
        anchor_message_ids=list(cluster.message_ids),
        in_day_message_ids=[item.message_id for item in selected_messages],
        base_message_ids=sorted(base_message_ids),
        messages=selected_messages,
        reply_relation_ids=reply_relation_ids,
        quote_relation_ids=quote_relation_ids,
        attachment_refs=_collect_attachment_refs(selected_messages),
        anchor_signals=anchor_signals,
    )


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
        related = {message.reply_to_message_id, message.quote_message_id}
        if message.message_id in anchor_ids:
            expanded.update(filter(None, related))
        elif any(target in anchor_ids for target in related if target):
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
        if message.sender_open_id != self_open_id:
            previous_was_self = False
            continue
        if previous_was_self and current:
            current.append(message.message_id)
        else:
            if current:
                clusters.append(RawAnchorCluster(message.conversation_id, current))
            current = [message.message_id]
        previous_was_self = True
    if current:
        clusters.append(RawAnchorCluster(messages[0].conversation_id if messages else "", current))
    return clusters


def _build_anchor_unit_id(conversation_id: str, anchor_message_ids: list[str]) -> str:
    return f"{conversation_id}:{'-'.join(anchor_message_ids)}"


def _collect_attachment_refs(messages: list[NormalizedMessage]) -> list[AttachmentMeta]:
    seen: set[str] = set()
    attachments: list[AttachmentMeta] = []
    for message in messages:
        for attachment in message.attachments:
            if attachment.attachment_id not in seen:
                seen.add(attachment.attachment_id)
                attachments.append(attachment)
    return attachments
