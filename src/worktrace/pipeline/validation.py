from __future__ import annotations

from hashlib import sha1

from ..constants import ContextRequestType
from ..errors import AnalyzerProtocolError
from ..models import (
    BatchAnalysisResult,
    CrossConversationGroup,
    CrossConversationGroupResult,
    ContextRequest,
    ConversationSlice,
    MergedEventDraft,
    SourceBackedEventDraft,
)
from ..utils.link_refs import build_message_link_candidates, sort_referenced_link_ids
from ..utils.hashing import stable_event_id


def normalize_source_message_ids(
    source_message_ids: list[str],
    conversation_slice: ConversationSlice,
) -> list[str]:
    allowed = [message.message_id for message in conversation_slice.messages]
    allowed_set = set(conversation_slice.in_day_message_ids)
    expanded_ids = set(source_message_ids)
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

    target_message_ids = [message_id.strip() for message_id in request.target_message_ids if message_id.strip()]
    if not target_message_ids:
        return False

    message_ids = {message.message_id for message in conversation_slice.messages}
    if not set(target_message_ids).issubset(message_ids):
        return False

    if request.request_type == ContextRequestType.ATTACHMENT_TEXT.value:
        valid_attachment_ids = {
            attachment.attachment_id
            for message in conversation_slice.messages
            if message.message_id in target_message_ids
            for attachment in message.attachments
        }
        return bool(request.target_attachment_ids) and set(request.target_attachment_ids).issubset(
            valid_attachment_ids
        )

    return not request.target_attachment_ids


def validate_batch_analysis_result(
    result: BatchAnalysisResult,
    slices_by_id: dict[str, ConversationSlice],
    *,
    self_open_id: str = "",
) -> BatchAnalysisResult:
    valid_candidates: list[SourceBackedEventDraft] = []
    valid_requests: list[ContextRequest] = []
    seen_draft_ids: set[str] = set()

    for candidate in result.candidate_events:
        conversation_slice = slices_by_id.get(candidate.source_slice_id)
        if conversation_slice is None and len(slices_by_id) == 1:
            conversation_slice = next(iter(slices_by_id.values()))
        if conversation_slice is None:
            continue
        source_conversation_id = candidate.source_conversation_id.strip() or conversation_slice.conversation_id
        source_slice_id = candidate.source_slice_id.strip() or conversation_slice.slice_id
        if source_conversation_id != conversation_slice.conversation_id:
            continue

        normalized_ids = normalize_source_message_ids(
            candidate.source_message_ids,
            conversation_slice,
        )
        if not normalized_ids:
            continue
        referenced_link_ids = _filter_valid_referenced_link_ids(
            candidate.referenced_link_ids,
            conversation_slice=conversation_slice,
            source_message_ids=normalized_ids,
        )

        draft_id = candidate.draft_id.strip()
        date = candidate.date.strip()
        if not date:
            date = _infer_target_date_from_slice(conversation_slice)
        if not draft_id:
            draft_id = stable_event_id(date, normalized_ids)
        if draft_id in seen_draft_ids:
            draft_id = _make_unique_draft_id(
                draft_id,
                self_open_id=self_open_id,
                title=candidate.topic,
                seen_draft_ids=seen_draft_ids,
            )
        seen_draft_ids.add(draft_id)

        valid_candidates.append(
            SourceBackedEventDraft(
                draft_id=draft_id,
                date=date,
                topic=candidate.topic,
                content=candidate.content,
                action_label=(candidate.action_label or "").strip(),
                object_hint=(candidate.object_hint or "").strip(),
                retention_reason=(candidate.retention_reason or "").strip(),
                retention_detail=(candidate.retention_detail or "").strip(),
                referenced_link_ids=referenced_link_ids,
                source_message_ids=normalized_ids,
                source_conversation_id=source_conversation_id,
                source_slice_id=source_slice_id,
                confidence=candidate.confidence,
            )
        )

    for request in result.context_requests:
        request_slice_id = request.slice_id.strip()
        conversation_slice = slices_by_id.get(request_slice_id)
        if conversation_slice is None and len(slices_by_id) == 1:
            conversation_slice = next(iter(slices_by_id.values()))
            request_slice_id = conversation_slice.slice_id
        if conversation_slice and validate_context_request_against_slice(request, conversation_slice):
            valid_requests.append(
                ContextRequest(
                    slice_id=request_slice_id,
                    request_type=request.request_type,
                    target_message_ids=request.target_message_ids,
                    target_attachment_ids=request.target_attachment_ids,
                    reason=request.reason,
                    limit=max(1, request.limit),
                )
            )

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
                object_hint=draft.object_hint,
                retention_reason=draft.retention_reason,
                retention_detail=draft.retention_detail,
                referenced_link_ids=sort_referenced_link_ids(
                    [
                        link_id
                        for link_id in draft.referenced_link_ids
                        if _link_id_belongs_to_messages(link_id, ordered_ids)
                    ],
                    message_order=ordered_ids,
                ),
                source_message_ids=ordered_ids,
                source_conversation_ids=sorted(set(draft.source_conversation_ids)),
            )
        )
    return normalized


def validate_cross_conversation_groups(
    group_result: CrossConversationGroupResult,
    candidates: list[SourceBackedEventDraft],
) -> CrossConversationGroupResult:
    expected = [candidate.draft_id for candidate in candidates]
    expected_set = set(expected)
    seen: set[str] = set()
    normalized_groups: list[CrossConversationGroup] = []
    duplicates: list[str] = []
    unknown: list[str] = []

    for group in group_result.groups:
        normalized_ids: list[str] = []
        for draft_id in group.draft_ids:
            if draft_id not in expected_set:
                unknown.append(draft_id)
                continue
            if draft_id in seen:
                duplicates.append(draft_id)
                continue
            seen.add(draft_id)
            normalized_ids.append(draft_id)

        if normalized_ids:
            normalized_groups.append(
                CrossConversationGroup(
                    group_id=group.group_id,
                    draft_ids=normalized_ids,
                )
            )

    missing = [draft_id for draft_id in expected if draft_id not in seen]
    if not missing and not unknown:
        return CrossConversationGroupResult(groups=normalized_groups)

    details: list[str] = []
    if missing:
        details.append(f"missing={missing}")
    if duplicates:
        details.append(f"duplicates={sorted(set(duplicates))}")
    if unknown:
        details.append(f"unknown={sorted(set(unknown))}")
    raise AnalyzerProtocolError(
        "Cross-conversation merge groups are invalid: " + "; ".join(details)
    )


def _filter_valid_referenced_link_ids(
    referenced_link_ids: list[str],
    *,
    conversation_slice: ConversationSlice,
    source_message_ids: list[str],
) -> list[str]:
    allowed_message_ids = set(source_message_ids)
    allowed_link_ids = {
        item.link_id
        for message in conversation_slice.messages
        if message.message_id in allowed_message_ids
        for item in build_message_link_candidates(message)
    }
    return [
        link_id
        for link_id in sort_referenced_link_ids(
            [link_id for link_id in referenced_link_ids if link_id in allowed_link_ids],
            message_order=source_message_ids,
        )
    ]


def _link_id_belongs_to_messages(link_id: str, message_ids: list[str]) -> bool:
    for candidate_message_id in message_ids:
        if link_id.startswith(f"{candidate_message_id}#link"):
            return True
    return False


def normalize_cross_conversation_groups_with_fallback(
    group_result: CrossConversationGroupResult,
    candidates: list[SourceBackedEventDraft],
) -> tuple[CrossConversationGroupResult, list[str]]:
    expected = [candidate.draft_id for candidate in candidates]
    expected_set = set(expected)
    seen: set[str] = set()
    normalized_groups: list[CrossConversationGroup] = []
    duplicates: list[str] = []
    unknown: list[str] = []

    for group in group_result.groups:
        normalized_ids: list[str] = []
        for draft_id in group.draft_ids:
            if draft_id not in expected_set:
                unknown.append(draft_id)
                continue
            if draft_id in seen:
                duplicates.append(draft_id)
                continue
            seen.add(draft_id)
            normalized_ids.append(draft_id)

        if normalized_ids:
            normalized_groups.append(
                CrossConversationGroup(
                    group_id=group.group_id,
                    draft_ids=normalized_ids,
                )
            )

    missing = [draft_id for draft_id in expected if draft_id not in seen]
    for index, draft_id in enumerate(missing, start=1):
        normalized_groups.append(
            CrossConversationGroup(
                group_id=f"fallback-{index}",
                draft_ids=[draft_id],
            )
        )

    warnings: list[str] = []
    if missing or duplicates or unknown:
        details: list[str] = []
        if missing:
            details.append(f"missing={missing}")
        if duplicates:
            details.append(f"duplicates={sorted(set(duplicates))}")
        if unknown:
            details.append(f"unknown={sorted(set(unknown))}")
        warnings.append(
            "Cross-conversation merge groups were repaired: " + "; ".join(details)
        )

    return CrossConversationGroupResult(groups=normalized_groups), warnings


def expect_json_object(payload: object, context: str) -> dict:
    if not isinstance(payload, dict):
        raise AnalyzerProtocolError(f"{context} must be a JSON object.")
    return payload


def _infer_target_date_from_slice(conversation_slice: ConversationSlice) -> str:
    if conversation_slice.messages:
        send_time = conversation_slice.messages[0].send_time.strip()
        if len(send_time) >= 10:
            return send_time[:10]
    return ""


def _make_unique_draft_id(
    base_draft_id: str,
    *,
    self_open_id: str,
    title: str,
    seen_draft_ids: set[str],
) -> str:
    suffix = sha1(f"{self_open_id}|{title}".encode("utf-8")).hexdigest()[:8]
    candidate = f"{base_draft_id}-{suffix}"
    if candidate not in seen_draft_ids:
        return candidate

    sequence = 2
    while True:
        candidate = f"{base_draft_id}-{suffix}-{sequence}"
        if candidate not in seen_draft_ids:
            return candidate
        sequence += 1
