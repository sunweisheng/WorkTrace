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
from ..reaction_catalog import ReactionCatalog, enrich_message_reactions
from ..sources.base import ChatSource
from ..utils.token_estimation import estimate_text_tokens


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
    reaction_catalog: ReactionCatalog | None = None,
    warning_sink: list[str] | None = None,
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

    related_requests: dict[ContextDirection, list[str]] = {
        ContextDirection.EARLIER: [],
        ContextDirection.LATER: [],
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
        related_requests[direction].extend(target_message_ids)

    temporal_message_ids: set[str] = set()
    relation_message_ids: set[str] = set()
    for direction, target_ids in related_requests.items():
        normalized_targets = _normalize_target_message_ids(target_ids)
        if not normalized_targets:
            continue
        related = chat_source.fetch_related_messages(
            conversation_slice.conversation_id,
            normalized_targets,
            direction,
            config.context_expansion_messages_per_direction,
        )
        if reaction_catalog is not None:
            related = enrich_message_reactions(related, reaction_catalog)
        for message in related:
            temporal_message_ids.add(message.message_id)
            message_by_id.setdefault(message.message_id, message)

    fetch_by_ids = getattr(chat_source, "fetch_messages_by_ids", None)
    if callable(fetch_by_ids) and temporal_message_ids:
        relation_targets = {
            relation_id
            for message_id in temporal_message_ids
            for relation_id in (
                message_by_id[message_id].reply_to_message_id,
                message_by_id[message_id].quote_message_id,
            )
            if relation_id and relation_id not in message_by_id
        }
        if relation_targets:
            related_objects = fetch_by_ids(
                conversation_slice.conversation_id,
                sorted(relation_targets),
            )
            if reaction_catalog is not None:
                related_objects = enrich_message_reactions(related_objects, reaction_catalog)
            for message in related_objects:
                relation_message_ids.add(message.message_id)
                message_by_id.setdefault(message.message_id, message)

    updated_messages = _limit_expanded_messages(
        original_messages=conversation_slice.messages,
        all_messages=message_by_id,
        temporal_message_ids=temporal_message_ids,
        relation_message_ids=relation_message_ids,
        config=config,
        warning_sink=warning_sink,
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


def _limit_expanded_messages(
    *,
    original_messages,
    all_messages,
    temporal_message_ids: set[str],
    relation_message_ids: set[str],
    config: RuntimeConfig,
    warning_sink: list[str] | None,
):
    selected_ids = {message.message_id for message in original_messages}
    candidates = [
        message
        for message in sorted(all_messages.values(), key=lambda item: (item.send_time, item.message_id))
        if message.message_id not in selected_ids
    ]
    ordered = [
        *[message for message in candidates if message.message_id in temporal_message_ids],
        *[
            message
            for message in candidates
            if message.message_id in relation_message_ids and message.message_id not in temporal_message_ids
        ],
    ]
    kept = list(original_messages)
    for message in ordered:
        proposal = sorted([*kept, message], key=lambda item: (item.send_time, item.message_id))
        if _estimate_messages_tokens(proposal, config) > config.max_model_input_tokens:
            if warning_sink is not None:
                warning_sink.append(
                    f"Skipped expanded context message because it exceeds the model input limit: {message.message_id}."
                )
            continue
        kept = proposal
    return sorted(
        kept,
        key=lambda item: (item.send_time, item.message_id),
    )


def _estimate_messages_tokens(messages, config: RuntimeConfig) -> int:
    from ..models import ConversationSlice

    return _estimate_slice_tokens(
        ConversationSlice(
            slice_id="context-limit",
            conversation_id=messages[0].conversation_id if messages else "",
            conversation_name=messages[0].conversation_name if messages else "",
            anchor_message_ids=[],
            in_day_message_ids=[message.message_id for message in messages],
            messages=messages,
        ),
        config,
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
    return estimate_text_tokens(serialized)
