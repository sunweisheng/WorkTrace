from __future__ import annotations

from ..constants import ContextRequestType
from ..errors import AnalyzerProtocolError
from ..models import (
    BatchAnalysisResult,
    ContextRequest,
    ConversationSlice,
    MergedEventDraft,
    SourceBackedEventDraft,
)
from ..analyzers.prompts import build_prompt_message_short_ids


def normalize_source_message_ids(
    source_message_ids: list[str],
    conversation_slice: ConversationSlice,
) -> list[str]:
    allowed = [message.message_id for message in conversation_slice.messages]
    allowed_set = set(conversation_slice.in_day_message_ids)
    short_id_map = build_prompt_message_short_ids(conversation_slice.messages)
    reverse_short_id_map = {short_id: message_id for message_id, short_id in short_id_map.items()}
    expanded_ids = {
        reverse_short_id_map.get(message_id, message_id)
        for message_id in source_message_ids
    }
    normalized: list[str] = []
    seen: set[str] = set()

    for message_id in allowed:
        if (
            message_id in expanded_ids
            and message_id in allowed_set
            and message_id not in seen
        ):
            normalized.append(message_id)
            seen.add(message_id)
    return normalized


def validate_context_request_against_slice(
    request: ContextRequest,
    conversation_slice: ConversationSlice,
) -> bool:
    if request.request_type not in {
        ContextRequestType.EARLIER_MESSAGES.value,
        ContextRequestType.LATER_MESSAGES.value,
        ContextRequestType.ATTACHMENT_TEXT.value,
    }:
        return False

    message_ids = {message.message_id for message in conversation_slice.messages}
    if not set(request.target_message_ids).issubset(message_ids):
        return False

    if request.request_type == ContextRequestType.ATTACHMENT_TEXT.value:
        valid_attachment_ids = {
            attachment.attachment_id
            for message in conversation_slice.messages
            if message.message_id in request.target_message_ids
            for attachment in message.attachments
        }
        return bool(request.target_attachment_ids) and set(request.target_attachment_ids).issubset(
            valid_attachment_ids
        )

    return not request.target_attachment_ids


def validate_batch_analysis_result(
    result: BatchAnalysisResult,
    slices_by_id: dict[str, ConversationSlice],
) -> BatchAnalysisResult:
    valid_candidates: list[SourceBackedEventDraft] = []
    valid_requests: list[ContextRequest] = []

    for candidate in result.candidate_events:
        conversation_slice = slices_by_id.get(candidate.source_slice_id)
        if conversation_slice is None:
            continue
        if candidate.source_conversation_id != conversation_slice.conversation_id:
            continue

        normalized_ids = normalize_source_message_ids(
            candidate.source_message_ids,
            conversation_slice,
        )
        if not normalized_ids:
            continue

        valid_candidates.append(
            SourceBackedEventDraft(
                draft_id=candidate.draft_id,
                date=candidate.date,
                topic=candidate.topic,
                content=candidate.content,
                result=candidate.result,
                source_message_ids=normalized_ids,
                source_conversation_id=candidate.source_conversation_id,
                source_slice_id=candidate.source_slice_id,
                confidence=candidate.confidence,
            )
        )

    for request in result.context_requests:
        conversation_slice = slices_by_id.get(request.slice_id)
        if conversation_slice and validate_context_request_against_slice(request, conversation_slice):
            valid_requests.append(request)

    return BatchAnalysisResult(
        candidate_events=valid_candidates,
        context_requests=valid_requests,
    )


def validate_merged_event_drafts(
    drafts: list[MergedEventDraft],
    *,
    message_order: list[str],
) -> list[MergedEventDraft]:
    allowed = set(message_order)
    normalized: list[MergedEventDraft] = []

    for draft in drafts:
        ordered_ids = [message_id for message_id in message_order if message_id in set(draft.source_message_ids)]
        ordered_ids = [message_id for message_id in ordered_ids if message_id in allowed]
        if not ordered_ids:
            continue
        normalized.append(
            MergedEventDraft(
                date=draft.date,
                topic=draft.topic,
                content=draft.content,
                result=draft.result,
                source_message_ids=ordered_ids,
                source_conversation_ids=sorted(set(draft.source_conversation_ids)),
            )
        )
    return normalized


def expect_json_object(payload: object, context: str) -> dict:
    if not isinstance(payload, dict):
        raise AnalyzerProtocolError(f"{context} must be a JSON object.")
    return payload
