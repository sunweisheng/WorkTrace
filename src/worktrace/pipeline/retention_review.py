from __future__ import annotations

from ..analyzers.output_schemas import retention_review_output_schema
from ..analyzers.prompts import build_retention_review_prompt
from ..config import RetentionPolicyConfig, RuntimeConfig
from ..errors import AnalyzerProtocolError, ModelInputLimitError
from ..models import (
    ConversationSlice,
    NormalizedMessage,
    RetentionReviewBatch,
    RetentionReviewCandidate,
    RetentionReviewItemResult,
    RetentionReviewResult,
    SourceBackedEventDraft,
)
from ..utils.token_estimation import estimate_model_input_tokens


def select_retention_review_candidates(
    candidates: list[SourceBackedEventDraft],
    policy: RetentionPolicyConfig,
) -> list[SourceBackedEventDraft]:
    if not policy.review_enabled:
        return []
    selected: list[SourceBackedEventDraft] = []
    allowed_reasons = set(policy.review_retention_reasons)
    for candidate in candidates:
        if candidate.retention_reason not in allowed_reasons:
            continue
        if policy.require_empty_workstream and candidate.workstream_key.strip():
            continue
        if policy.require_no_referenced_files and (
            candidate.referenced_link_ids or candidate.referenced_attachment_ids
        ):
            continue
        selected.append(candidate)
    return selected


def build_retention_review_candidates(
    candidates: list[SourceBackedEventDraft],
    *,
    slices: list[ConversationSlice],
    messages: list[NormalizedMessage],
) -> list[RetentionReviewCandidate]:
    slices_by_id = {item.slice_id: item for item in slices}
    messages_by_id = {item.message_id: item for item in messages}
    review_candidates: list[RetentionReviewCandidate] = []
    for candidate in candidates:
        conversation_slice = slices_by_id.get(candidate.source_slice_id)
        if conversation_slice is not None:
            allowed_ids = list(
                conversation_slice.primary_message_ids
                or conversation_slice.in_day_message_ids
            )
            review_messages = list(conversation_slice.messages)
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
            message_id
            for message_id in allowed_ids
            if any(item.message_id == message_id for item in review_messages)
        ]
        if not allowed_ids or not review_messages:
            raise AnalyzerProtocolError(
                "Retention review candidate has no source-backed messages: "
                f"{candidate.draft_id}."
            )
        review_candidates.append(
            RetentionReviewCandidate(
                candidate=candidate,
                messages=review_messages,
                allowed_evidence_message_ids=allowed_ids,
            )
        )
    return review_candidates


def pack_retention_review_batches(
    *,
    target_date: str,
    candidates: list[RetentionReviewCandidate],
    config: RuntimeConfig,
) -> list[RetentionReviewBatch]:
    batches: list[RetentionReviewBatch] = []
    current: list[RetentionReviewCandidate] = []
    for candidate in candidates:
        proposal = [*current, candidate]
        probe = RetentionReviewBatch(
            target_date=target_date,
            batch_id=f"retention-review-{len(batches) + 1:03d}",
            candidates=proposal,
        )
        if (
            current
            and _estimate_review_prompt_tokens(probe, config)
            > config.max_model_input_tokens
        ):
            batches.append(
                RetentionReviewBatch(
                    target_date=target_date,
                    batch_id=f"retention-review-{len(batches) + 1:03d}",
                    candidates=current,
                )
            )
            current = [candidate]
            continue
        current = proposal
    if current:
        batches.append(
            RetentionReviewBatch(
                target_date=target_date,
                batch_id=f"retention-review-{len(batches) + 1:03d}",
                candidates=current,
            )
        )
    for batch in batches:
        estimated_tokens = _estimate_review_prompt_tokens(batch, config)
        if estimated_tokens > config.max_model_input_tokens:
            raise ModelInputLimitError(
                "Retention review prompt exceeds max_model_input_tokens: "
                f"batch={batch.batch_id} estimated_tokens={estimated_tokens} "
                f"limit={config.max_model_input_tokens}."
            )
    return batches


def validate_retention_review_result(
    batch: RetentionReviewBatch,
    result: RetentionReviewResult,
    policy: RetentionPolicyConfig,
) -> dict[str, RetentionReviewItemResult]:
    expected = [item.candidate.draft_id for item in batch.candidates]
    returned = [item.draft_id for item in result.results]
    if returned != expected:
        raise AnalyzerProtocolError(
            "Retention review must return every draft_id once and in batch order."
        )

    routine_types = {item.key for item in policy.routine_signals}
    substantive_types = {item.key for item in policy.substantive_signals}
    candidates_by_id = {
        item.candidate.draft_id: item for item in batch.candidates
    }
    validated: dict[str, RetentionReviewItemResult] = {}
    for item in result.results:
        candidate = candidates_by_id[item.draft_id]
        allowed_ids = set(candidate.allowed_evidence_message_ids)
        for signals, allowed_types in (
            (item.routine_signals, routine_types),
            (item.substantive_signals, substantive_types),
        ):
            for signal in signals:
                evidence_ids = signal.evidence_message_ids
                if (
                    signal.signal_type not in allowed_types
                    or not evidence_ids
                    or not set(evidence_ids).issubset(allowed_ids)
                ):
                    raise AnalyzerProtocolError(
                        "Retention review returned an invalid signal or evidence reference."
                    )
        validated[item.draft_id] = item
    return validated


def apply_retention_review_results(
    candidates: list[SourceBackedEventDraft],
    reviewed: dict[str, RetentionReviewItemResult],
    policy: RetentionPolicyConfig,
) -> tuple[list[SourceBackedEventDraft], int, int, int]:
    kept: list[SourceBackedEventDraft] = []
    kept_reviewed_count = 0
    dropped_routine_count = 0
    dropped_uncertain_count = 0
    for candidate in candidates:
        result = reviewed.get(candidate.draft_id)
        if result is None:
            kept.append(candidate)
            continue
        if result.substantive_signals:
            kept.append(candidate)
            kept_reviewed_count += 1
            continue
        if result.routine_signals:
            dropped_routine_count += 1
            continue
        if policy.uncertain_policy == "keep":
            kept.append(candidate)
            kept_reviewed_count += 1
        else:
            dropped_uncertain_count += 1
    return (
        kept,
        kept_reviewed_count,
        dropped_routine_count,
        dropped_uncertain_count,
    )


def _estimate_review_prompt_tokens(
    batch: RetentionReviewBatch,
    config: RuntimeConfig,
) -> int:
    return estimate_model_input_tokens(
        build_retention_review_prompt(batch, config=config),
        output_schema=retention_review_output_schema(config),
        append_no_think=True,
    )
