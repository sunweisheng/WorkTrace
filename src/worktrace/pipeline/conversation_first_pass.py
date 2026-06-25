from __future__ import annotations

from ..config import RuntimeConfig
from ..models import ConversationSlice, NormalizedMessage
from .slicing import build_slice_id


def build_conversation_level_slices(
    messages: list[NormalizedMessage],
    self_open_id: str,
    config: RuntimeConfig,
) -> list[ConversationSlice]:
    by_conversation: dict[str, list[NormalizedMessage]] = {}
    for message in messages:
        by_conversation.setdefault(message.conversation_id, []).append(message)

    slices: list[ConversationSlice] = []
    for conversation_id, conversation_messages in sorted(by_conversation.items()):
        anchor_message_ids = [
            message.message_id
            for message in conversation_messages
            if message.sender_open_id == self_open_id
        ]
        if not anchor_message_ids:
            continue

        limited_messages = _trim_conversation_messages(
            conversation_messages,
            anchor_ids=set(anchor_message_ids),
            max_messages=config.slice_base_limit,
        )
        kept_anchor_ids = [
            message_id
            for message_id in anchor_message_ids
            if any(item.message_id == message_id for item in limited_messages)
        ]
        if not kept_anchor_ids:
            continue

        slices.append(
            ConversationSlice(
                slice_id=build_slice_id(conversation_id, kept_anchor_ids),
                conversation_id=conversation_id,
                conversation_name=conversation_messages[0].conversation_name
                if conversation_messages
                else "",
                anchor_message_ids=kept_anchor_ids,
                in_day_message_ids=[message.message_id for message in limited_messages],
                messages=limited_messages,
                attachment_texts=[],
            )
        )
    return slices


def _trim_conversation_messages(
    messages: list[NormalizedMessage],
    *,
    anchor_ids: set[str],
    max_messages: int,
) -> list[NormalizedMessage]:
    if len(messages) <= max_messages:
        return list(messages)

    scored: list[tuple[int, int, NormalizedMessage]] = []
    index_by_id = {message.message_id: index for index, message in enumerate(messages)}
    anchor_indexes = [
        index_by_id[message_id] for message_id in anchor_ids if message_id in index_by_id
    ]
    anchor_parent_ids = {
        target_id
        for message in messages
        if message.message_id in anchor_ids
        for target_id in (message.reply_to_message_id, message.quote_message_id)
        if target_id
    }
    for index, message in enumerate(messages):
        scored.append(
            (
                *_message_priority(
                    message,
                    message_index=index,
                    anchor_ids=anchor_ids,
                    anchor_indexes=anchor_indexes,
                    anchor_parent_ids=anchor_parent_ids,
                ),
                message,
            )
        )

    selected_indexes = {
        index
        for _, _, index, _ in sorted(scored, key=lambda item: (item[0], item[1], item[2]))[
            :max_messages
        ]
    }
    return [message for index, message in enumerate(messages) if index in selected_indexes]


def _message_priority(
    message: NormalizedMessage,
    *,
    message_index: int,
    anchor_ids: set[str],
    anchor_indexes: list[int],
    anchor_parent_ids: set[str],
) -> tuple[int, int, int]:
    if message.message_id in anchor_ids:
        return (0, 0, message_index)
    if message.message_id in anchor_parent_ids:
        return (1, _min_anchor_distance(message_index, anchor_indexes), message_index)
    if (
        message.reply_to_message_id in anchor_ids
        or message.quote_message_id in anchor_ids
    ):
        return (2, _min_anchor_distance(message_index, anchor_indexes), message_index)
    if (
        message.sender_open_id is not None
        and (
            message.reply_to_message_id is not None
            or message.quote_message_id is not None
        )
    ):
        return (4, _min_anchor_distance(message_index, anchor_indexes), message_index)
    if message.sender_open_id is None:
        return (6, _min_anchor_distance(message_index, anchor_indexes), message_index)
    return (5, _min_anchor_distance(message_index, anchor_indexes), message_index)


def _min_anchor_distance(message_index: int, anchor_indexes: list[int]) -> int:
    if not anchor_indexes:
        return 999999
    return min(abs(message_index - anchor_index) for anchor_index in anchor_indexes)
