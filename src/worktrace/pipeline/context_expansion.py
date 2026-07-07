from __future__ import annotations

from dataclasses import replace

from ..config import RuntimeConfig
from ..constants import ContextDirection, ContextRequestType
from ..models import (
    AnalysisBatch,
    AttachmentTextBlock,
    ContextRequest,
    ConversationSlice,
    LinkedFileTextBlock,
)
from ..resolvers.base import ContentResolver
from ..sources.base import ChatSource


def _normalize_target_message_ids(message_ids: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for message_id in message_ids:
        trimmed = message_id.strip()
        if not trimmed or trimmed in seen:
            continue
        seen.add(trimmed)
        normalized.append(trimmed)
    return normalized


def expand_slice_context(
    conversation_slice: ConversationSlice,
    requests: list[ContextRequest],
    *,
    chat_source: ChatSource,
    content_resolver: ContentResolver,
    config: RuntimeConfig,
) -> ConversationSlice:
    if not requests:
        return conversation_slice

    request_order = {
        ContextRequestType.ATTACHMENT_TEXT.value: 0,
        ContextRequestType.LINKED_FILE_TEXT.value: 1,
        ContextRequestType.EARLIER_MESSAGES.value: 2,
        ContextRequestType.LATER_MESSAGES.value: 3,
    }
    ordered = sorted(requests, key=lambda item: request_order[item.request_type])

    message_by_id = {message.message_id: message for message in conversation_slice.messages}
    attachment_blocks: dict[str, AttachmentTextBlock] = {
        block.attachment_id: block for block in conversation_slice.attachment_texts
    }
    linked_file_blocks: dict[str, LinkedFileTextBlock] = {
        block.link_id: block for block in conversation_slice.linked_file_texts
    }

    for request in ordered:
        if request.request_type == ContextRequestType.ATTACHMENT_TEXT.value:
            for message_id in _normalize_target_message_ids(request.target_message_ids):
                message = message_by_id.get(message_id)
                if not message:
                    continue
                loaded = content_resolver.load_attachment_text_if_needed(
                    message,
                    request.target_attachment_ids,
                    request.reason,
                )
                for block in loaded or []:
                    attachment_blocks.setdefault(block.attachment_id, block)
            continue
        if request.request_type == ContextRequestType.LINKED_FILE_TEXT.value:
            for message_id in _normalize_target_message_ids(request.target_message_ids):
                message = message_by_id.get(message_id)
                if not message:
                    continue
                loaded = content_resolver.load_link_text_if_needed(
                    message,
                    request.target_link_ids,
                    request.reason,
                )
                for block in loaded or []:
                    linked_file_blocks.setdefault(block.link_id, block)
            continue

        direction = (
            ContextDirection.EARLIER
            if request.request_type == ContextRequestType.EARLIER_MESSAGES.value
            else ContextDirection.LATER
        )
        target_message_ids = _normalize_target_message_ids(request.target_message_ids)
        if not target_message_ids:
            continue
        related = chat_source.fetch_related_messages(
            conversation_slice.conversation_id,
            target_message_ids,
            direction,
            min(request.limit, config.max_model_input_tokens),
        )
        for message in related:
            message_by_id.setdefault(message.message_id, message)

    updated_messages = sorted(
        message_by_id.values(),
        key=lambda item: (item.send_time, item.message_id),
    )
    return replace(
        conversation_slice,
        messages=updated_messages,
        attachment_texts=sorted(
            attachment_blocks.values(),
            key=lambda item: (item.message_id, item.attachment_id),
        ),
        linked_file_texts=sorted(
            linked_file_blocks.values(),
            key=lambda item: (item.message_id, item.link_id),
        ),
    )


def build_single_slice_retry_batch(
    target_date: str,
    conversation_slice: ConversationSlice,
    *,
    retry_round: int,
    self_open_id: str,
    self_display_name: str,
    config: RuntimeConfig | None = None,
) -> AnalysisBatch:
    runtime_config = config or RuntimeConfig()
    estimated_tokens = _estimate_slice_tokens(conversation_slice, runtime_config)
    return AnalysisBatch(
        target_date=target_date,
        batch_id=f"{conversation_slice.slice_id}-retry-{retry_round}",
        retry_round=retry_round,
        estimated_tokens=estimated_tokens,
        self_open_id=self_open_id,
        self_display_name=self_display_name,
        slices=[conversation_slice],
    )


def _estimate_slice_tokens(
    conversation_slice: ConversationSlice,
    config: RuntimeConfig,
) -> int:
    import json

    from ..analyzers.prompts import serialize_slice_for_prompt

    payload = serialize_slice_for_prompt(conversation_slice, config)
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return max(1, len(serialized) // 3 + 50)
