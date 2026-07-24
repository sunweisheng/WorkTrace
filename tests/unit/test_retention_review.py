from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from src.worktrace.analyzers.output_schemas import retention_review_output_schema
from src.worktrace.analyzers.prompts import build_retention_review_prompt
from src.worktrace.config import RuntimeConfig, load_runtime_config_overrides
from src.worktrace.errors import AnalyzerProtocolError
from src.worktrace.models import (
    ConversationSlice,
    NormalizedMessage,
    RetentionReviewItemResult,
    RetentionReviewBatch,
    RetentionReviewResult,
    RetentionSignalEvidence,
    SourceBackedEventDraft,
)
from src.worktrace.pipeline.retention_review import (
    apply_retention_review_results,
    build_retention_review_candidates,
    pack_retention_review_batches,
    select_retention_review_candidates,
    validate_retention_review_result,
)
from src.worktrace.utils.token_estimation import estimate_model_input_tokens


CONFIG = load_runtime_config_overrides(RuntimeConfig(), cwd=Path.cwd())
POLICY = CONFIG.retention_policy


def _message(message_id: str, text: str, *, sender: str = "ou_self") -> NormalizedMessage:
    return NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="协作群",
        message_id=message_id,
        sender_open_id=sender,
        sender_name="本人" if sender == "ou_self" else "同事",
        send_time="2026-07-15T09:00:00+08:00",
        message_type="text",
        text=text,
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )


def _candidate(
    draft_id: str = "d1",
    *,
    reason: str = "follow_up_assigned",
    links: list[str] | None = None,
    attachments: list[str] | None = None,
) -> SourceBackedEventDraft:
    return SourceBackedEventDraft(
        draft_id=draft_id,
        date="2026-07-15",
        topic="协助确认同事工作状态",
        content="响应同事请求并反馈当前状态。",
        source_message_ids=["m1", "m2"],
        source_conversation_id="oc_1",
        source_slice_id="s1",
        confidence=0.9,
        action_label="确认",
        object_hint="相关同事",
        retention_reason=reason,
        retention_detail="响应同事请求并完成信息反馈。",
        referenced_link_ids=links or [],
        referenced_attachment_ids=attachments or [],
        self_evidence_message_ids=["m2"],
    )


def _slice() -> ConversationSlice:
    messages = [
        _message("m1", "帮我确认一下相关同事是否在工位", sender="ou_other"),
        _message("m2", "我还在路上，稍后再看"),
    ]
    return ConversationSlice(
        slice_id="s1",
        conversation_id="oc_1",
        conversation_name="协作群",
        anchor_message_ids=["m2"],
        in_day_message_ids=["m1", "m2"],
        messages=messages,
        primary_message_ids=["m1", "m2"],
        context_message_ids=[],
        self_evidence_message_ids=["m2"],
    )


def _review_batch(candidate: SourceBackedEventDraft | None = None):
    selected = [candidate or _candidate()]
    items = build_retention_review_candidates(
        selected,
        slices=[_slice()],
        messages=_slice().messages,
    )
    return pack_retention_review_batches(
        target_date="2026-07-15",
        candidates=items,
        config=CONFIG,
    )[0]


def test_selects_only_configured_structural_boundary_candidates() -> None:
    selected = select_retention_review_candidates(
        [
            _candidate("selected"),
            _candidate("decision", reason="decision_made"),
            _candidate("linked", links=["m1#link1"]),
            _candidate("attached", attachments=["att-1"]),
        ],
        POLICY,
    )

    assert [item.draft_id for item in selected] == ["selected"]


def test_review_prompt_asks_for_signals_not_keep_or_drop() -> None:
    prompt = build_retention_review_prompt(_review_batch(), config=CONFIG)

    assert "candidate_summary 仅用于定位候选，不能作为语义证据" in prompt
    assert '"presence_or_availability"' in prompt
    assert '"explicit_business_follow_up"' in prompt
    assert "不要决定保留或删除" in prompt
    assert '"allowed_evidence_message_ids"' in prompt


def test_validates_source_backed_review_signals() -> None:
    batch = _review_batch()
    result = RetentionReviewResult(
        results=[
            RetentionReviewItemResult(
                draft_id="d1",
                routine_signals=[
                    RetentionSignalEvidence(
                        signal_type="presence_or_availability",
                        evidence_message_ids=["m1", "m2"],
                    )
                ],
            )
        ]
    )

    validated = validate_retention_review_result(batch, result, POLICY)

    assert list(validated) == ["d1"]


@pytest.mark.parametrize(
    "result",
    [
        RetentionReviewResult(results=[]),
        RetentionReviewResult(
            results=[
                RetentionReviewItemResult(
                    draft_id="d1",
                    routine_signals=[
                        RetentionSignalEvidence(
                            signal_type="presence_or_availability",
                            evidence_message_ids=["unknown"],
                        )
                    ],
                )
            ]
        ),
    ],
)
def test_rejects_missing_or_unbacked_review_results(
    result: RetentionReviewResult,
) -> None:
    with pytest.raises(AnalyzerProtocolError):
        validate_retention_review_result(_review_batch(), result, POLICY)


def test_substantive_signal_wins_over_routine_signal() -> None:
    candidate = _candidate()
    reviewed = {
        "d1": RetentionReviewItemResult(
            draft_id="d1",
            routine_signals=[
                RetentionSignalEvidence("presence_or_availability", ["m1"])
            ],
            substantive_signals=[
                RetentionSignalEvidence("explicit_business_follow_up", ["m1"])
            ],
        )
    }

    kept, kept_count, dropped_routine, dropped_uncertain = (
        apply_retention_review_results([candidate], reviewed, POLICY)
    )

    assert kept == [candidate]
    assert (kept_count, dropped_routine, dropped_uncertain) == (1, 0, 0)


@pytest.mark.parametrize(
    "signal_type",
    [item.key for item in POLICY.routine_signals],
)
def test_every_configured_routine_signal_drops_without_substantive_work(
    signal_type: str,
) -> None:
    candidate = _candidate()
    reviewed = {
        "d1": RetentionReviewItemResult(
            draft_id="d1",
            routine_signals=[RetentionSignalEvidence(signal_type, ["m1"])],
        )
    }

    kept, kept_count, dropped_routine, dropped_uncertain = (
        apply_retention_review_results([candidate], reviewed, POLICY)
    )

    assert kept == []
    assert (kept_count, dropped_routine, dropped_uncertain) == (0, 1, 0)


@pytest.mark.parametrize(
    "signal_type",
    [item.key for item in POLICY.substantive_signals],
)
def test_every_configured_substantive_signal_keeps_the_candidate(
    signal_type: str,
) -> None:
    candidate = _candidate()
    reviewed = {
        "d1": RetentionReviewItemResult(
            draft_id="d1",
            substantive_signals=[RetentionSignalEvidence(signal_type, ["m1"])],
        )
    }

    kept, kept_count, dropped_routine, dropped_uncertain = (
        apply_retention_review_results([candidate], reviewed, POLICY)
    )

    assert kept == [candidate]
    assert (kept_count, dropped_routine, dropped_uncertain) == (1, 0, 0)


def test_routine_and_uncertain_candidates_are_dropped_by_policy() -> None:
    routine = _candidate("routine")
    uncertain = _candidate("uncertain")
    reviewed = {
        "routine": RetentionReviewItemResult(
            draft_id="routine",
            routine_signals=[
                RetentionSignalEvidence("information_relay_only", ["m1"])
            ],
        ),
        "uncertain": RetentionReviewItemResult(draft_id="uncertain"),
    }

    kept, kept_count, dropped_routine, dropped_uncertain = (
        apply_retention_review_results(
            [routine, uncertain],
            reviewed,
            POLICY,
        )
    )

    assert kept == []
    assert (kept_count, dropped_routine, dropped_uncertain) == (0, 1, 1)


def test_review_batches_split_before_the_model_token_limit() -> None:
    first_batch = _review_batch(_candidate("d1"))
    first_item = first_batch.candidates[0]
    second_item = replace(
        first_item,
        candidate=replace(first_item.candidate, draft_id="d2"),
    )
    single_probe = RetentionReviewBatch(
        target_date="2026-07-15",
        batch_id="probe",
        candidates=[first_item],
    )
    single_tokens = estimate_model_input_tokens(
        build_retention_review_prompt(single_probe, config=CONFIG),
        output_schema=retention_review_output_schema(CONFIG),
        append_no_think=True,
    )
    limited_config = replace(CONFIG, model_input_batch_target_tokens=single_tokens + 10)

    batches = pack_retention_review_batches(
        target_date="2026-07-15",
        candidates=[first_item, second_item],
        config=limited_config,
    )

    assert [len(batch.candidates) for batch in batches] == [1, 1]
    for batch in batches:
        estimated_tokens = estimate_model_input_tokens(
            build_retention_review_prompt(batch, config=limited_config),
            output_schema=retention_review_output_schema(limited_config),
            append_no_think=True,
        )
        assert estimated_tokens <= limited_config.model_input_batch_target_tokens


def test_single_oversized_review_candidate_is_marked_for_model_invocation() -> None:
    item = _review_batch().candidates[0]

    batches = pack_retention_review_batches(
        target_date="2026-07-15",
        candidates=[item],
        config=replace(CONFIG, model_input_batch_target_tokens=1),
    )

    assert len(batches) == 1
    assert batches[0].oversized_singleton is True
    assert batches[0].estimated_input_tokens > batches[0].input_target_tokens
