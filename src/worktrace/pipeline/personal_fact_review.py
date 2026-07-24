from __future__ import annotations

from dataclasses import replace

from ..analyzers.output_schemas import personal_fact_review_output_schema
from ..analyzers.function_calls import message_reference_ids, task_function_call_spec
from ..analyzers.prompts import build_personal_fact_review_prompt
from ..config import RetentionPolicyConfig, RuntimeConfig
from ..errors import AnalyzerProtocolError
from ..models import (
    ConversationSlice,
    NormalizedMessage,
    PersonalFactItem,
    PersonalFactReviewBatch,
    PersonalFactReviewCandidate,
    PersonalFactReviewItemResult,
    PersonalFactReviewResult,
    SourceBackedEventDraft,
)
from ..utils.text import clean_text
from ..utils.token_estimation import estimate_structured_input_tokens
from .validation import PERSONAL_FACT_FIELDS


_FACT_FIELD_ORDER = (
    "topic",
    "content",
    "action_label",
    "object_hint",
    "retention_detail",
)


def build_personal_fact_review_candidates(
    candidates: list[SourceBackedEventDraft],
    *,
    slices: list[ConversationSlice],
    messages: list[NormalizedMessage],
    policy: RetentionPolicyConfig,
) -> list[PersonalFactReviewCandidate]:
    if not policy.fact_review_enabled:
        return []

    slices_by_id = {item.slice_id: item for item in slices}
    messages_by_id = {item.message_id: item for item in messages}
    configured_risk_flags = {item.key for item in policy.fact_risk_signals}
    selected: list[PersonalFactReviewCandidate] = []

    for candidate in candidates:
        conversation_slice = slices_by_id.get(candidate.source_slice_id)
        if conversation_slice is not None:
            review_messages = list(conversation_slice.messages)
            preferred_ids = set(
                conversation_slice.primary_message_ids
                or conversation_slice.in_day_message_ids
            )
            preferred_ids.update(candidate.source_message_ids)
            preferred_ids.update(candidate.self_evidence_message_ids)
            allowed_ids = [
                message.message_id
                for message in review_messages
                if message.message_id in preferred_ids
            ]
        else:
            allowed_ids = list(
                dict.fromkeys(
                    [
                        *candidate.source_message_ids,
                        *candidate.self_evidence_message_ids,
                    ]
                )
            )
            review_messages = [
                messages_by_id[message_id]
                for message_id in allowed_ids
                if message_id in messages_by_id
            ]
            allowed_ids = [
                message_id for message_id in allowed_ids if message_id in messages_by_id
            ]

        source_messages = [
            message
            for message in review_messages
            if message.message_id in set(candidate.source_message_ids)
        ]
        participant_count = len(
            {
                message.sender_open_id
                for message in source_messages
                if message.sender_open_id
            }
        )
        review_reasons: list[str] = []
        if not personal_fact_evidence_is_complete(candidate):
            review_reasons.append("missing_or_incomplete_fact_evidence")
        if (
            len(candidate.source_message_ids)
            >= policy.fact_review_source_message_count
        ):
            review_reasons.append("source_message_count")
        if participant_count >= policy.fact_review_source_participant_count:
            review_reasons.append("source_participant_count")
        review_reasons.extend(
            f"risk_flag:{flag}"
            for flag in dict.fromkeys(candidate.fact_risk_flags)
            if flag in configured_risk_flags
        )
        if not review_reasons:
            continue
        if not allowed_ids or not review_messages:
            raise AnalyzerProtocolError(
                "Personal fact review candidate has no source-backed messages: "
                f"{candidate.draft_id}."
            )
        selected.append(
            PersonalFactReviewCandidate(
                candidate=candidate,
                messages=review_messages,
                allowed_evidence_message_ids=allowed_ids,
                review_reasons=review_reasons,
            )
        )
    return selected


def pack_personal_fact_review_batches(
    *,
    target_date: str,
    candidates: list[PersonalFactReviewCandidate],
    config: RuntimeConfig,
) -> list[PersonalFactReviewBatch]:
    batches: list[PersonalFactReviewBatch] = []
    current: list[PersonalFactReviewCandidate] = []
    for candidate in candidates:
        proposal = [*current, candidate]
        probe = PersonalFactReviewBatch(
            target_date=target_date,
            batch_id=f"personal-fact-review-{len(batches) + 1:03d}",
            candidates=proposal,
        )
        if (
            current
            and (
                len(proposal)
                > config.retention_policy.fact_review_max_batch_candidates
                or _estimate_personal_fact_review_tokens(probe, config)
                > config.model_input_batch_target_tokens
            )
        ):
            batches.append(
                PersonalFactReviewBatch(
                    target_date=target_date,
                    batch_id=f"personal-fact-review-{len(batches) + 1:03d}",
                    candidates=current,
                )
            )
            current = [candidate]
            continue
        current = proposal
    if current:
        batches.append(
            PersonalFactReviewBatch(
                target_date=target_date,
                batch_id=f"personal-fact-review-{len(batches) + 1:03d}",
                candidates=current,
            )
        )

    marked_batches: list[PersonalFactReviewBatch] = []
    for batch in batches:
        estimated_tokens = _estimate_personal_fact_review_tokens(batch, config)
        marked_batches.append(
            PersonalFactReviewBatch(
                target_date=batch.target_date,
                batch_id=batch.batch_id,
                candidates=list(batch.candidates),
                retry_feedback=batch.retry_feedback,
                estimated_input_tokens=estimated_tokens,
                input_target_tokens=config.model_input_batch_target_tokens,
                oversized_singleton=(
                    len(batch.candidates) == 1
                    and estimated_tokens > config.model_input_batch_target_tokens
                ),
            )
        )
    return marked_batches


def validate_personal_fact_review_result(
    batch: PersonalFactReviewBatch,
    result: PersonalFactReviewResult,
) -> dict[str, PersonalFactReviewItemResult]:
    expected = [item.candidate.draft_id for item in batch.candidates]
    returned = [item.draft_id for item in result.results]
    if returned != expected:
        raise AnalyzerProtocolError(
            "Personal fact review must return every draft_id once and in batch order."
        )

    candidates_by_id = {
        item.candidate.draft_id: item for item in batch.candidates
    }
    validated: dict[str, PersonalFactReviewItemResult] = {}
    for item in result.results:
        review_candidate = candidates_by_id[item.draft_id]
        text_fields = _fact_text_fields(item)
        if not item.supported:
            if any(clean_text(value) for value in text_fields.values()):
                raise AnalyzerProtocolError(
                    "Unsupported personal fact review result must clear all text fields."
                )
            if item.fact_items or not any(clean_text(value) for value in item.removed_claims):
                raise AnalyzerProtocolError(
                    "Unsupported personal fact review result must include removed claims only."
                )
            validated[item.draft_id] = item
            continue

        required_field_names = (
            "topic",
            "content",
            "object_hint",
            "retention_detail",
        )
        missing_field_names = [
            field_name
            for field_name in required_field_names
            if not clean_text(text_fields[field_name])
        ]
        if missing_field_names:
            raise AnalyzerProtocolError(
                "Supported personal fact review result is missing required event fields: "
                + ", ".join(missing_field_names)
                + ". Return supported=false when legal evidence cannot support every "
                "required field."
            )
        try:
            fact_items = validate_personal_fact_items(
                text_fields,
                item.fact_items,
                allowed_message_ids=review_candidate.allowed_evidence_message_ids,
            )
        except AnalyzerProtocolError as exc:
            raise AnalyzerProtocolError(
                f"Personal fact review draft_id={item.draft_id}: {exc}"
            ) from exc
        validated[item.draft_id] = replace(item, fact_items=fact_items)
    return validated


def apply_personal_fact_review_results(
    candidates: list[SourceBackedEventDraft],
    review_candidates: list[PersonalFactReviewCandidate],
    reviewed: dict[str, PersonalFactReviewItemResult],
    policy: RetentionPolicyConfig,
) -> tuple[list[SourceBackedEventDraft], int, int, int]:
    review_candidates_by_id = {
        item.candidate.draft_id: item for item in review_candidates
    }
    kept: list[SourceBackedEventDraft] = []
    confirmed_count = 0
    revised_count = 0
    dropped_count = 0

    for candidate in candidates:
        result = reviewed.get(candidate.draft_id)
        if result is None:
            kept.append(candidate)
            continue
        if not result.supported:
            if policy.fact_review_unsupported_policy == "fail":
                raise AnalyzerProtocolError(
                    "Personal fact review found a candidate without supported facts: "
                    f"{candidate.draft_id}."
                )
            dropped_count += 1
            continue

        review_candidate = review_candidates_by_id[candidate.draft_id]
        evidence_ids = {
            message_id
            for fact in result.fact_items
            for message_id in fact.evidence_message_ids
        }
        source_ids = set(candidate.source_message_ids) | evidence_ids
        ordered_source_ids = [
            message_id
            for message_id in review_candidate.allowed_evidence_message_ids
            if message_id in source_ids
        ]
        ordered_source_ids.extend(
            message_id
            for message_id in candidate.source_message_ids
            if message_id not in set(ordered_source_ids)
        )
        revised = replace(
            candidate,
            topic=clean_text(result.topic),
            content=clean_text(result.content),
            action_label=clean_text(result.action_label),
            object_hint=clean_text(result.object_hint),
            retention_detail=clean_text(result.retention_detail),
            source_message_ids=ordered_source_ids,
            fact_items=list(result.fact_items),
            fact_risk_flags=[],
        )
        if _fact_text_fields_from_candidate(revised) == _fact_text_fields_from_candidate(
            candidate
        ):
            confirmed_count += 1
        else:
            revised_count += 1
        kept.append(revised)

    return kept, confirmed_count, revised_count, dropped_count


def personal_fact_evidence_is_complete(candidate: SourceBackedEventDraft) -> bool:
    try:
        validate_personal_fact_items(
            _fact_text_fields_from_candidate(candidate),
            candidate.fact_items,
            allowed_message_ids=candidate.source_message_ids,
        )
    except AnalyzerProtocolError:
        return False
    return True


def validate_personal_fact_items(
    text_fields: dict[str, str],
    fact_items: list[PersonalFactItem],
    *,
    allowed_message_ids: list[str],
) -> list[PersonalFactItem]:
    allowed = set(allowed_message_ids)
    message_order = {message_id: index for index, message_id in enumerate(allowed_message_ids)}
    normalized: list[PersonalFactItem] = []
    by_field: dict[str, list[PersonalFactItem]] = {}
    for item in fact_items:
        field_name = item.field_name.strip()
        text = clean_text(item.text)
        evidence_ids = list(dict.fromkeys(item.evidence_message_ids))
        if field_name not in PERSONAL_FACT_FIELDS:
            raise AnalyzerProtocolError(
                f"Personal fact item has an invalid field: {field_name}."
            )
        if not text:
            raise AnalyzerProtocolError(
                f"Personal fact item has empty text for field {field_name}."
            )
        if not evidence_ids:
            raise AnalyzerProtocolError(
                f"Personal fact item has no evidence for field {field_name}."
            )
        invalid_evidence_ids = [
            message_id for message_id in evidence_ids if message_id not in allowed
        ]
        if invalid_evidence_ids:
            raise AnalyzerProtocolError(
                "Personal fact item references evidence outside this candidate's "
                "allowed_evidence_message_ids: "
                + ", ".join(invalid_evidence_ids)
                + "."
            )
        evidence_ids.sort(key=lambda value: message_order[value])
        normalized_item = PersonalFactItem(
            field_name=field_name,
            text=text,
            evidence_message_ids=evidence_ids,
        )
        normalized.append(normalized_item)
        by_field.setdefault(field_name, []).append(normalized_item)

    for field_name in _FACT_FIELD_ORDER:
        field_text = clean_text(text_fields.get(field_name, ""))
        items = by_field.get(field_name, [])
        if not field_text:
            if items:
                raise AnalyzerProtocolError(
                    "Personal fact items include evidence for empty field "
                    f"{field_name}."
                )
            continue
        if field_name == "content":
            if not items or clean_text("".join(item.text for item in items)) != field_text:
                raise AnalyzerProtocolError(
                    "Personal content fact items do not exactly cover field content."
                )
            continue
        if len(items) != 1 or items[0].text != field_text:
            raise AnalyzerProtocolError(
                "Personal fact items do not exactly cover field "
                f"{field_name}."
            )
    return normalized


def _fact_text_fields(item: PersonalFactReviewItemResult) -> dict[str, str]:
    return {
        "topic": item.topic,
        "content": item.content,
        "action_label": item.action_label,
        "object_hint": item.object_hint,
        "retention_detail": item.retention_detail,
    }


def _fact_text_fields_from_candidate(
    candidate: SourceBackedEventDraft,
) -> dict[str, str]:
    return {
        "topic": clean_text(candidate.topic),
        "content": clean_text(candidate.content),
        "action_label": clean_text(candidate.action_label),
        "object_hint": clean_text(candidate.object_hint),
        "retention_detail": clean_text(candidate.retention_detail),
    }


def _estimate_personal_fact_review_tokens(
    batch: PersonalFactReviewBatch,
    config: RuntimeConfig,
) -> int:
    references = message_reference_ids(
        [message for item in batch.candidates for message in item.messages]
    )
    function_spec = task_function_call_spec(
        "personal_fact_review",
        personal_fact_review_output_schema(batch),
        draft_ids=[item.candidate.draft_id for item in batch.candidates],
        result_count=len(batch.candidates),
        **references,
    )
    return estimate_structured_input_tokens(
        build_personal_fact_review_prompt(batch, config=config),
        function_spec=function_spec,
        append_no_think=True,
    )["input_estimated_tokens"]
