from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from ..models import AnchorSignal, AnchorUnit, NormalizedMessage
from ..reaction_catalog import ReactionCatalog
from ..sources.base import ChatSource


@dataclass(frozen=True)
class _Anchor:
    message_ids: list[str]
    signals: list[AnchorSignal]


def build_initial_anchor_windows(
    messages: list[NormalizedMessage],
    self_open_id: str,
    *,
    max_anchor_gap_minutes: int,
    max_unrelated_intervening_messages: int,
    initial_context_messages_before: int,
    reaction_catalog: ReactionCatalog | None = None,
) -> list[AnchorUnit]:
    """Build deterministic, same-day windows before the model asks for extra context."""
    by_conversation: dict[str, list[NormalizedMessage]] = {}
    for message in messages:
        by_conversation.setdefault(message.conversation_id, []).append(message)
    catalog = reaction_catalog or ReactionCatalog.empty("")
    windows: list[AnchorUnit] = []
    for conversation_id, raw_messages in by_conversation.items():
        timeline = sorted(raw_messages, key=lambda item: (item.send_time, item.message_id))
        anchors = _build_anchors(timeline, self_open_id=self_open_id, catalog=catalog)
        if _is_private_conversation(timeline):
            windows.extend(_build_private_conversation_window(conversation_id, timeline, anchors))
            continue
        windows.extend(
            _build_conversation_windows(
                conversation_id,
                timeline,
                anchors,
                self_open_id=self_open_id,
                max_anchor_gap_minutes=max_anchor_gap_minutes,
                max_unrelated_intervening_messages=max_unrelated_intervening_messages,
                initial_context_messages_before=initial_context_messages_before,
            )
        )
    return windows


def _is_private_conversation(messages: list[NormalizedMessage]) -> bool:
    return any(message.conversation_mode == "p2p" for message in messages)


def _build_private_conversation_window(
    conversation_id: str,
    messages: list[NormalizedMessage],
    anchors: list[_Anchor],
) -> list[AnchorUnit]:
    if not anchors:
        return []
    anchor_ids = list(
        dict.fromkeys(message_id for anchor in anchors for message_id in anchor.message_ids)
    )
    signals = sorted(
        [signal for anchor in anchors for signal in anchor.signals],
        key=lambda item: (item.action_time, item.signal_id),
    )
    return [
        AnchorUnit(
            anchor_unit_id=f"{conversation_id}:private-001",
            conversation_id=conversation_id,
            conversation_name=messages[0].conversation_name if messages else "",
            anchor_message_ids=anchor_ids,
            in_day_message_ids=[message.message_id for message in messages],
            base_message_ids=[message.message_id for message in messages],
            messages=messages,
            anchor_signals=signals,
        )
    ]


def append_private_window_external_relations(
    windows: list[AnchorUnit],
    *,
    chat_source: ChatSource,
    reaction_catalog: ReactionCatalog | None = None,
) -> list[AnchorUnit]:
    """Append one-hop reply and quote context that sits outside the target day."""
    fetch_by_ids = getattr(chat_source, "fetch_messages_by_ids", None)
    if not callable(fetch_by_ids):
        return windows
    catalog = reaction_catalog or ReactionCatalog.empty("")
    hydrated: list[AnchorUnit] = []
    for window in windows:
        if not window.anchor_unit_id.endswith(":private-001"):
            hydrated.append(window)
            continue
        main_ids = set(window.base_message_ids)
        current_by_id = {message.message_id: message for message in window.messages}
        fetched = fetch_by_ids(window.conversation_id, list(window.base_message_ids))
        fetched = [
            message
            for message in fetched
            if message.message_id not in current_by_id
            and (
                message.reply_to_message_id in main_ids
                or message.quote_message_id in main_ids
            )
        ]
        parent_ids = {
            relation_id
            for message in [*window.messages, *fetched]
            for relation_id in (message.reply_to_message_id, message.quote_message_id)
            if relation_id and relation_id not in current_by_id
        }
        parents = fetch_by_ids(window.conversation_id, sorted(parent_ids)) if parent_ids else []
        additions = [*fetched, *parents]
        if reaction_catalog is not None:
            from ..reaction_catalog import enrich_message_reactions

            additions = enrich_message_reactions(additions, catalog)
        added_ids: list[str] = []
        for message in additions:
            if message.message_id in current_by_id:
                continue
            current_by_id[message.message_id] = message
            added_ids.append(message.message_id)
        if not added_ids:
            hydrated.append(window)
            continue
        hydrated.append(
            replace(
                window,
                messages=sorted(
                    current_by_id.values(), key=lambda item: (item.send_time, item.message_id)
                ),
                relation_context_message_ids=list(
                    dict.fromkeys([*window.relation_context_message_ids, *added_ids])
                ),
            )
        )
    return hydrated


def _build_anchors(
    messages: list[NormalizedMessage], *, self_open_id: str, catalog: ReactionCatalog
) -> list[_Anchor]:
    text_anchors: list[_Anchor] = []
    current_ids: list[str] = []
    for message in messages:
        if message.sender_open_id == self_open_id:
            current_ids.append(message.message_id)
        elif current_ids:
            text_anchors.append(_Anchor(current_ids, []))
            current_ids = []
    if current_ids:
        text_anchors.append(_Anchor(current_ids, []))

    by_message: dict[str, _Anchor] = {
        message_id: anchor for anchor in text_anchors for message_id in anchor.message_ids
    }
    standalone: list[_Anchor] = []
    seen: set[str] = set()
    for message in messages:
        for index, reaction in enumerate(message.reactions):
            if reaction.operator_open_id != self_open_id:
                continue
            signal_id = reaction.reaction_id or f"reaction:{message.message_id}:{index}"
            if signal_id in seen:
                continue
            seen.add(signal_id)
            metadata = catalog.lookup(reaction.emoji_type)
            signal = AnchorSignal(
                signal_id=signal_id,
                kind="reaction",
                message_id=message.message_id,
                action_time=reaction.action_time or message.send_time,
                emoji_type=reaction.emoji_type,
                emoji_name=metadata.name,
                emoji_description=metadata.description,
                semantic=metadata.semantic,
            )
            anchor = by_message.get(message.message_id)
            if anchor is None:
                standalone.append(_Anchor([message.message_id], [signal]))
            else:
                anchor.signals.append(signal)
    for anchor in text_anchors:
        anchor.signals.extend(
            AnchorSignal(
                signal_id=f"text:{message_id}",
                kind="text",
                message_id=message_id,
                action_time=next(item.send_time for item in messages if item.message_id == message_id),
            )
            for message_id in anchor.message_ids
        )
    indexes = {item.message_id: index for index, item in enumerate(messages)}
    return sorted(text_anchors + standalone, key=lambda item: min(indexes[mid] for mid in item.message_ids))


def _build_conversation_windows(
    conversation_id: str,
    messages: list[NormalizedMessage],
    anchors: list[_Anchor],
    *,
    self_open_id: str,
    max_anchor_gap_minutes: int,
    max_unrelated_intervening_messages: int,
    initial_context_messages_before: int,
) -> list[AnchorUnit]:
    if not anchors:
        return []
    index_by_id = {message.message_id: index for index, message in enumerate(messages)}
    groups: list[list[_Anchor]] = [[anchors[0]]]
    for previous, current in zip(anchors, anchors[1:]):
        if _starts_new_window(
            previous,
            current,
            messages=messages,
            index_by_id=index_by_id,
            self_open_id=self_open_id,
            max_anchor_gap_minutes=max_anchor_gap_minutes,
            max_unrelated_intervening_messages=max_unrelated_intervening_messages,
        ):
            groups.append([current])
        else:
            groups[-1].append(current)

    results: list[AnchorUnit] = []
    for number, group in enumerate(groups, start=1):
        anchor_ids = [message_id for anchor in group for message_id in anchor.message_ids]
        first = min(index_by_id[item] for item in anchor_ids)
        last = max(index_by_id[item] for item in anchor_ids)
        main = messages[first : last + 1]
        main_ids = {message.message_id for message in main}
        timeline_context = messages[
            max(0, first - initial_context_messages_before) : first
        ]
        referenced_parent_ids = {
            relation_id
            for message in main
            for relation_id in (message.reply_to_message_id, message.quote_message_id)
            if relation_id
        }
        relation = [
            message
            for message in messages
            if message.message_id not in main_ids
            and (
                message.reply_to_message_id in main_ids
                or message.quote_message_id in main_ids
                or message.message_id in referenced_parent_ids
            )
        ]
        selected_by_id = {
            message.message_id: message for message in [*timeline_context, *main, *relation]
        }
        selected = sorted(selected_by_id.values(), key=lambda item: (item.send_time, item.message_id))
        signals = sorted(
            [signal for anchor in group for signal in anchor.signals],
            key=lambda item: (item.action_time, item.signal_id),
        )
        results.append(
            AnchorUnit(
                anchor_unit_id=f"{conversation_id}:window-{number:03d}",
                conversation_id=conversation_id,
                conversation_name=messages[0].conversation_name if messages else "",
                anchor_message_ids=anchor_ids,
                in_day_message_ids=[message.message_id for message in main],
                base_message_ids=[message.message_id for message in main],
                messages=selected,
                relation_context_message_ids=[message.message_id for message in relation],
                timeline_context_message_ids=[
                    message.message_id for message in timeline_context
                ],
                reply_relation_ids=sorted(
                    {message.reply_to_message_id for message in relation if message.reply_to_message_id}
                ),
                quote_relation_ids=sorted(
                    {message.quote_message_id for message in relation if message.quote_message_id}
                ),
                anchor_signals=signals,
            )
        )
    return results


def _starts_new_window(
    previous: _Anchor,
    current: _Anchor,
    *,
    messages: list[NormalizedMessage],
    index_by_id: dict[str, int],
    self_open_id: str,
    max_anchor_gap_minutes: int,
    max_unrelated_intervening_messages: int,
) -> bool:
    previous_last = max(index_by_id[item] for item in previous.message_ids)
    current_first = min(index_by_id[item] for item in current.message_ids)
    previous_time = _parse_time(messages[previous_last].send_time)
    current_time = _parse_time(messages[current_first].send_time)
    if (current_time - previous_time).total_seconds() / 60 > max_anchor_gap_minutes:
        return True
    related_targets = set(previous.message_ids) | set(current.message_ids)
    unrelated = 0
    for message in messages[previous_last + 1 : current_first]:
        if message.sender_open_id == self_open_id:
            continue
        if (
            message.reply_to_message_id in related_targets
            or message.quote_message_id in related_targets
            or self_open_id in message.mentioned_open_ids
        ):
            continue
        unrelated += 1
    return unrelated > max_unrelated_intervening_messages


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)
