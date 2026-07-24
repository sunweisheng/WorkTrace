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
    PersonalFactItem,
    SelfRelationEvidence,
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
        ContextRequestType.LINKED_FILE_TEXT.value,
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
        return (
            bool(request.target_attachment_ids)
            and set(request.target_attachment_ids).issubset(valid_attachment_ids)
            and not request.target_link_ids
        )
    if request.request_type == ContextRequestType.LINKED_FILE_TEXT.value:
        valid_link_ids = {
            item.link_id
            for message in conversation_slice.messages
            if message.message_id in target_message_ids
            for item in build_message_link_candidates(message)
        }
        return (
            bool(request.target_link_ids)
            and set(request.target_link_ids).issubset(valid_link_ids)
            and not request.target_attachment_ids
        )

    return not request.target_attachment_ids and not request.target_link_ids


def validate_batch_analysis_result(
    result: BatchAnalysisResult,
    slices_by_id: dict[str, ConversationSlice],
    *,
    self_open_id: str = "",
    self_relation_keys: tuple[str, ...] = (),
    fact_risk_keys: tuple[str, ...] = (),
    warning_sink: list[str] | None = None,
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
        referenced_attachment_ids = _filter_valid_referenced_attachment_ids(
            candidate.referenced_attachment_ids,
            conversation_slice=conversation_slice,
            source_message_ids=normalized_ids,
        )
        self_evidence_message_ids = _filter_valid_self_evidence_message_ids(
            candidate.self_evidence_message_ids,
            conversation_slice=conversation_slice,
            self_open_id=self_open_id,
        )
        self_relations = _filter_valid_self_relations(
            candidate.self_relations,
            allowed_relations=self_relation_keys,
            valid_self_evidence_message_ids=self_evidence_message_ids,
            candidate_id=candidate.draft_id,
            warning_sink=warning_sink,
        )
        fact_items = normalize_personal_fact_items(
            candidate.fact_items,
            allowed_message_ids=normalized_ids,
        )
        allowed_fact_risk_keys = set(fact_risk_keys)
        fact_risk_flags = [
            value
            for value in dict.fromkeys(candidate.fact_risk_flags)
            if value in allowed_fact_risk_keys
        ]

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
                referenced_attachment_ids=referenced_attachment_ids,
                self_evidence_message_ids=self_evidence_message_ids,
                self_relations=self_relations,
                source_message_ids=normalized_ids,
                source_conversation_id=source_conversation_id,
                source_slice_id=source_slice_id,
                confidence=candidate.confidence,
                response_outcome=candidate.response_outcome,
                response_signal_ids=list(candidate.response_signal_ids),
                response_evidence_message_ids=list(
                    candidate.response_evidence_message_ids
                ),
                fact_items=fact_items,
                fact_risk_flags=fact_risk_flags,
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
                    target_link_ids=request.target_link_ids,
                    reason=request.reason,
                    limit=max(1, request.limit),
                )
            )

    return BatchAnalysisResult(
        candidate_events=valid_candidates,
        context_requests=valid_requests,
    )


PERSONAL_FACT_FIELDS = {
    "topic",
    "content",
    "action_label",
    "object_hint",
    "retention_detail",
}


def normalize_personal_fact_items(
    fact_items: list[PersonalFactItem],
    *,
    allowed_message_ids: list[str],
) -> list[PersonalFactItem]:
    allowed = set(allowed_message_ids)
    message_order = {message_id: index for index, message_id in enumerate(allowed_message_ids)}
    normalized: list[PersonalFactItem] = []
    for item in fact_items:
        field_name = item.field_name.strip()
        text = item.text.strip()
        evidence_ids = list(dict.fromkeys(item.evidence_message_ids))
        if (
            field_name not in PERSONAL_FACT_FIELDS
            or not text
            or not evidence_ids
            or not set(evidence_ids).issubset(allowed)
        ):
            continue
        evidence_ids.sort(key=lambda value: message_order[value])
        normalized.append(
            PersonalFactItem(
                field_name=field_name,
                text=text,
                evidence_message_ids=evidence_ids,
            )
        )
    return normalized


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
                referenced_attachment_ids=list(
                    dict.fromkeys(draft.referenced_attachment_ids)
                ),
                action_labels=list(dict.fromkeys(draft.action_labels)),
                self_relations=list(dict.fromkeys(draft.self_relations)),
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
    candidate_by_id = {candidate.draft_id: candidate for candidate in candidates}
    candidate_order = {draft_id: index for index, draft_id in enumerate(expected)}
    seen: set[str] = set()
    normalized_groups: list[CrossConversationGroup] = []
    duplicates: list[str] = []
    unknown: list[str] = []
    invalid_primary: list[str] = []
    missing_reason: list[str] = []
    invalid_evidence: list[str] = []

    for group_index, group in enumerate(group_result.groups, start=1):
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
            normalized_ids.sort(key=candidate_order.__getitem__)
            if group.primary_draft_id not in normalized_ids:
                invalid_primary.append(group.primary_draft_id or f"group-{group_index}")
            allowed_evidence = {
                message_id
                for draft_id in normalized_ids
                for message_id in candidate_by_id[draft_id].source_message_ids
            }
            evidence_ids = list(dict.fromkeys(group.evidence_message_ids))
            invalid_group_evidence = [
                message_id
                for message_id in evidence_ids
                if message_id not in allowed_evidence
            ]
            if len(normalized_ids) > 1 and not group.merge_reason.strip():
                missing_reason.append(f"group-{group_index}")
            if len(normalized_ids) > 1 and not evidence_ids:
                invalid_evidence.append(f"group-{group_index}:missing")
            if invalid_group_evidence:
                invalid_evidence.extend(invalid_group_evidence)
            normalized_groups.append(
                CrossConversationGroup(
                    group_id=f"group-{len(normalized_groups) + 1:03d}",
                    draft_ids=normalized_ids,
                    primary_draft_id=group.primary_draft_id,
                    merge_reason=(
                        group.merge_reason.strip()
                        if len(normalized_ids) > 1
                        else "单条保留"
                    ),
                    evidence_message_ids=(
                        evidence_ids if len(normalized_ids) > 1 else []
                    ),
                )
            )

    missing = [draft_id for draft_id in expected if draft_id not in seen]
    if not any(
        (
            missing,
            duplicates,
            unknown,
            invalid_primary,
            missing_reason,
            invalid_evidence,
        )
    ):
        return CrossConversationGroupResult(groups=normalized_groups)

    details: list[str] = []
    if missing:
        details.append(f"missing={missing}")
    if duplicates:
        details.append(f"duplicates={sorted(set(duplicates))}")
    if unknown:
        details.append(f"unknown={sorted(set(unknown))}")
    if invalid_primary:
        details.append(f"invalid_primary={invalid_primary}")
    if missing_reason:
        details.append(f"missing_merge_reason={missing_reason}")
    if invalid_evidence:
        details.append(f"invalid_evidence={invalid_evidence}")
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


def _filter_valid_referenced_attachment_ids(
    attachment_ids: list[str],
    *,
    conversation_slice: ConversationSlice,
    source_message_ids: list[str],
) -> list[str]:
    source_id_set = set(source_message_ids)
    available_ids = {
        attachment.attachment_id
        for message in conversation_slice.messages
        if message.message_id in source_id_set
        for attachment in message.attachments
    }
    available_ids.update(
        {
            block.attachment_id
            for block in conversation_slice.attachment_texts
            if block.message_id in source_id_set
        }
    )
    return [
        attachment_id
        for attachment_id in dict.fromkeys(attachment_ids)
        if attachment_id in available_ids
    ]


def _filter_valid_self_evidence_message_ids(
    message_ids: list[str],
    *,
    conversation_slice: ConversationSlice,
    self_open_id: str,
) -> list[str]:
    message_by_id = {
        message.message_id: message for message in conversation_slice.messages
    }
    return [
        message_id
        for message_id in dict.fromkeys(message_ids)
        if message_id in message_by_id
        and (
            not self_open_id
            or message_by_id[message_id].sender_open_id == self_open_id
        )
    ]


def _filter_valid_self_relations(
    items: list[SelfRelationEvidence],
    *,
    allowed_relations: tuple[str, ...],
    valid_self_evidence_message_ids: list[str],
    candidate_id: str,
    warning_sink: list[str] | None,
) -> list[SelfRelationEvidence]:
    allowed = set(allowed_relations)
    valid_evidence = set(valid_self_evidence_message_ids)
    evidence_by_relation: dict[str, list[str]] = {}

    for item in items:
        relation = item.relation.strip()
        evidence_ids = list(dict.fromkeys(item.evidence_message_ids))
        if relation not in allowed:
            _append_self_relation_warning(
                warning_sink,
                candidate_id,
                f"unsupported relation `{relation or '<empty>'}`",
            )
            continue
        if not evidence_ids or not set(evidence_ids).issubset(valid_evidence):
            _append_self_relation_warning(
                warning_sink,
                candidate_id,
                "evidence is missing, outside the current segment, or not sent by self",
            )
            continue
        relation_evidence = evidence_by_relation.setdefault(relation, [])
        relation_evidence.extend(
            message_id
            for message_id in evidence_ids
            if message_id not in relation_evidence
        )

    return [
        SelfRelationEvidence(
            relation=relation,
            evidence_message_ids=evidence_by_relation[relation],
        )
        for relation in allowed_relations
        if relation in evidence_by_relation
    ]


def _append_self_relation_warning(
    warning_sink: list[str] | None,
    candidate_id: str,
    reason: str,
) -> None:
    if warning_sink is None:
        return
    warning_sink.append(
        f"Ignored invalid self relation for candidate {candidate_id or '<unknown>'}: {reason}."
    )


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
    candidate_by_id = {candidate.draft_id: candidate for candidate in candidates}
    candidate_order = {draft_id: index for index, draft_id in enumerate(expected)}
    seen: set[str] = set()
    normalized_groups: list[CrossConversationGroup] = []
    rejected_groups: list[int] = []

    for group_index, group in enumerate(group_result.groups, start=1):
        draft_ids = list(dict.fromkeys(group.draft_ids))
        allowed_evidence = {
            message_id
            for draft_id in draft_ids
            if draft_id in candidate_by_id
            for message_id in candidate_by_id[draft_id].source_message_ids
        }
        is_valid = bool(draft_ids) and all(
            draft_id in expected_set and draft_id not in seen
            for draft_id in draft_ids
        )
        is_valid = is_valid and len(draft_ids) == len(group.draft_ids)
        is_valid = is_valid and group.primary_draft_id in draft_ids
        if len(draft_ids) > 1:
            is_valid = is_valid and bool(group.merge_reason.strip())
            is_valid = is_valid and bool(group.evidence_message_ids)
            is_valid = is_valid and set(group.evidence_message_ids).issubset(
                allowed_evidence
            )
        if not is_valid:
            rejected_groups.append(group_index)
            continue
        draft_ids.sort(key=candidate_order.__getitem__)
        seen.update(draft_ids)
        normalized_groups.append(
            CrossConversationGroup(
                group_id="",
                draft_ids=draft_ids,
                primary_draft_id=group.primary_draft_id,
                merge_reason=(
                    group.merge_reason.strip() if len(draft_ids) > 1 else "单条保留"
                ),
                evidence_message_ids=(
                    list(dict.fromkeys(group.evidence_message_ids))
                    if len(draft_ids) > 1
                    else []
                ),
            )
        )

    missing = [draft_id for draft_id in expected if draft_id not in seen]
    for index, draft_id in enumerate(missing, start=1):
        normalized_groups.append(
            CrossConversationGroup(
                group_id=f"fallback-{index}",
                draft_ids=[draft_id],
                primary_draft_id=draft_id,
                merge_reason="单条保留",
                evidence_message_ids=[],
            )
        )

    normalized_groups.sort(
        key=lambda group: min(candidate_order[draft_id] for draft_id in group.draft_ids)
    )
    normalized_groups = [
        CrossConversationGroup(
            group_id=f"group-{index:03d}",
            draft_ids=group.draft_ids,
            primary_draft_id=group.primary_draft_id,
            merge_reason=group.merge_reason,
            evidence_message_ids=group.evidence_message_ids,
        )
        for index, group in enumerate(normalized_groups, start=1)
    ]
    warnings = [
        "Cross-conversation merge groups were repaired: "
        f"rejected_groups={rejected_groups}; singleton_candidates={missing}"
    ]

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
