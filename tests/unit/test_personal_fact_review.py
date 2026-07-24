from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from src.worktrace.analyzers.prompts import build_personal_fact_review_prompt
from src.worktrace.analyzers.function_calls import task_function_call_spec
from src.worktrace.analyzers.output_schemas import personal_fact_review_output_schema
from src.worktrace.config import RuntimeConfig, load_runtime_config_overrides
from src.worktrace.errors import AnalyzerProtocolError
from src.worktrace.models import (
    ConversationSlice,
    NormalizedMessage,
    PersonalFactItem,
    PersonalFactReviewBatch,
    PersonalFactReviewItemResult,
    PersonalFactReviewResult,
    SourceBackedEventDraft,
)
from src.worktrace.pipeline.personal_fact_review import (
    apply_personal_fact_review_results,
    build_personal_fact_review_candidates,
    pack_personal_fact_review_batches,
    personal_fact_evidence_is_complete,
    validate_personal_fact_review_result,
)


CONFIG = load_runtime_config_overrides(RuntimeConfig(), cwd=Path.cwd())
POLICY = CONFIG.retention_policy


def _message(message_id: str, text: str, sender: str) -> NormalizedMessage:
    return NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="设备协作群",
        message_id=message_id,
        sender_open_id=sender,
        sender_name=sender,
        send_time="2026-07-15T09:00:00+08:00",
        message_type="text",
        text=text,
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )


def _messages() -> list[NormalizedMessage]:
    return [
        _message("m1", "请修改三台设备的发货单信息", "ou_initiator"),
        _message("m2", "先核对设备编号再签收", "ou_executor"),
        _message("m3", "其中两台编号修改没有生效", "ou_initiator"),
        _message("m4", "我重新修改", "ou_executor"),
        _message("m5", "测试后没有重置，仍归属测试网点", "ou_executor"),
        _message("m6", "另一个地区之前也出现过类似情况", "ou_observer"),
        _message("m7", "已经完成后台重置", "ou_executor"),
        _message("m8", "验证正常，继续安排签收", "ou_initiator"),
    ]


def _slice() -> ConversationSlice:
    messages = _messages()
    ids = [item.message_id for item in messages]
    return ConversationSlice(
        slice_id="s1",
        conversation_id="oc_1",
        conversation_name="设备协作群",
        anchor_message_ids=["m1", "m3", "m8"],
        in_day_message_ids=ids,
        messages=messages,
        primary_message_ids=ids,
        context_message_ids=[],
        self_evidence_message_ids=["m1", "m3", "m8"],
    )


def _candidate(*, with_facts: bool = False) -> SourceBackedEventDraft:
    topic = "三台设备发货信息修改及归属重置"
    content = "修改三台设备的发货单信息，重新处理未生效的编号，并在后台重置测试网点归属后继续安排签收。"
    action = "修改并核对"
    object_hint = "三台设备发货单"
    detail = "发起人反馈编号修改未生效，执行人重新修改并完成归属重置，验证后继续签收。"
    fact_items = []
    if with_facts:
        fact_items = [
            PersonalFactItem("topic", topic, ["m1", "m5", "m7"]),
            PersonalFactItem("content", content, ["m1", "m3", "m4", "m5", "m7", "m8"]),
            PersonalFactItem("action_label", action, ["m4", "m7"]),
            PersonalFactItem("object_hint", object_hint, ["m1"]),
            PersonalFactItem("retention_detail", detail, ["m3", "m4", "m7", "m8"]),
        ]
    return SourceBackedEventDraft(
        draft_id="d1",
        date="2026-07-15",
        topic=topic,
        content=content,
        source_message_ids=[item.message_id for item in _messages()],
        source_conversation_id="oc_1",
        source_slice_id="s1",
        confidence=0.9,
        action_label=action,
        object_hint=object_hint,
        retention_reason="deliverable_updated",
        retention_detail=detail,
        self_evidence_message_ids=["m1", "m3", "m8"],
        fact_items=fact_items,
    )


def _review_result() -> PersonalFactReviewResult:
    candidate = _candidate(with_facts=True)
    return PersonalFactReviewResult(
        results=[
            PersonalFactReviewItemResult(
                draft_id="d1",
                supported=True,
                topic=candidate.topic,
                content=candidate.content,
                action_label=candidate.action_label,
                object_hint=candidate.object_hint,
                retention_detail=candidate.retention_detail,
                fact_items=candidate.fact_items,
                removed_claims=[
                    "将对比地区写成实际处理对象",
                    "将发起人写成全部操作的执行人",
                    "补充原聊天没有提出的流程建议",
                ],
            )
        ]
    )


def test_complete_low_risk_fact_evidence_does_not_trigger_extra_review() -> None:
    short_candidate = replace(
        _candidate(with_facts=True),
        source_message_ids=["m1", "m2"],
        fact_items=[
            PersonalFactItem("topic", "核对设备编号", ["m2"]),
            PersonalFactItem("content", "核对设备编号后再签收。", ["m2"]),
            PersonalFactItem("action_label", "核对", ["m2"]),
            PersonalFactItem("object_hint", "设备编号", ["m2"]),
            PersonalFactItem("retention_detail", "执行人要求先核对编号再签收。", ["m2"]),
        ],
        topic="核对设备编号",
        content="核对设备编号后再签收。",
        action_label="核对",
        object_hint="设备编号",
        retention_detail="执行人要求先核对编号再签收。",
    )

    selected = build_personal_fact_review_candidates(
        [short_candidate],
        slices=[_slice()],
        messages=_messages(),
        policy=POLICY,
    )

    assert personal_fact_evidence_is_complete(short_candidate)
    assert selected == []


def test_structural_thresholds_select_complex_event_without_reading_text() -> None:
    selected = build_personal_fact_review_candidates(
        [_candidate(with_facts=True)],
        slices=[_slice()],
        messages=_messages(),
        policy=POLICY,
    )

    assert len(selected) == 1
    assert selected[0].review_reasons == [
        "source_message_count",
        "source_participant_count",
    ]


def test_review_repairs_comparison_role_and_inferred_claims_without_dropping_event() -> None:
    incorrect = replace(
        _candidate(),
        topic="示例地区设备流程审核与修改",
        content="重置示例地区设备归属并提出流程变更建议。",
        action_label="重置归属并提出流程建议",
        object_hint="示例地区设备流程",
        retention_detail="发起人完成后台重置并提出流程调整。",
        fact_risk_flags=[
            "comparison_or_example",
            "role_or_responsibility_attribution",
            "inferred_decision_or_recommendation",
        ],
    )
    review_candidate = build_personal_fact_review_candidates(
        [incorrect],
        slices=[_slice()],
        messages=_messages(),
        policy=POLICY,
    )[0]
    batch = PersonalFactReviewBatch(
        target_date="2026-07-15",
        batch_id="personal-fact-review-001",
        candidates=[review_candidate],
    )
    validated = validate_personal_fact_review_result(batch, _review_result())

    kept, confirmed, revised, dropped = apply_personal_fact_review_results(
        [incorrect],
        [review_candidate],
        validated,
        POLICY,
    )

    assert (confirmed, revised, dropped) == (0, 1, 0)
    assert len(kept) == 1
    assert kept[0].topic == "三台设备发货信息修改及归属重置"
    assert "示例地区" not in kept[0].content
    assert "流程变更建议" not in kept[0].content
    assert "执行人重新修改并完成归属重置" in kept[0].retention_detail


def test_review_rejects_fact_evidence_outside_current_chat() -> None:
    review_candidate = build_personal_fact_review_candidates(
        [_candidate()],
        slices=[_slice()],
        messages=_messages(),
        policy=POLICY,
    )[0]
    batch = PersonalFactReviewBatch(
        target_date="2026-07-15",
        batch_id="personal-fact-review-001",
        candidates=[review_candidate],
    )
    result = _review_result()
    invalid_item = replace(
        result.results[0].fact_items[0],
        evidence_message_ids=["unknown"],
    )
    invalid_result = replace(
        result,
        results=[
            replace(
                result.results[0],
                fact_items=[invalid_item, *result.results[0].fact_items[1:]],
            )
        ],
    )

    with pytest.raises(
        AnalyzerProtocolError,
        match="outside this candidate's allowed_evidence_message_ids: unknown",
    ):
        validate_personal_fact_review_result(batch, invalid_result)


def test_review_reports_missing_required_field_and_false_fallback() -> None:
    review_candidate = build_personal_fact_review_candidates(
        [_candidate()],
        slices=[_slice()],
        messages=_messages(),
        policy=POLICY,
    )[0]
    batch = PersonalFactReviewBatch(
        target_date="2026-07-15",
        batch_id="personal-fact-review-001",
        candidates=[review_candidate],
    )
    result = _review_result()
    missing_object_result = replace(
        result,
        results=[
            replace(
                result.results[0],
                object_hint="",
                fact_items=[
                    item
                    for item in result.results[0].fact_items
                    if item.field_name != "object_hint"
                ],
            )
        ],
    )

    with pytest.raises(
        AnalyzerProtocolError,
        match=(
            "missing required event fields: object_hint.*"
            "Return supported=false"
        ),
    ):
        validate_personal_fact_review_result(batch, missing_object_result)


def test_single_oversized_fact_review_is_marked_for_model_invocation() -> None:
    review_candidates = build_personal_fact_review_candidates(
        [_candidate()],
        slices=[_slice()],
        messages=_messages(),
        policy=POLICY,
    )

    batches = pack_personal_fact_review_batches(
        target_date="2026-07-15",
        candidates=review_candidates,
        config=replace(CONFIG, model_input_batch_target_tokens=1),
    )

    assert len(batches) == 1
    assert batches[0].oversized_singleton is True
    assert batches[0].estimated_input_tokens > batches[0].input_target_tokens


def test_review_batches_obey_candidate_limit() -> None:
    review_candidate = build_personal_fact_review_candidates(
        [_candidate()],
        slices=[_slice()],
        messages=_messages(),
        policy=POLICY,
    )[0]
    second_candidate = replace(
        review_candidate,
        candidate=replace(review_candidate.candidate, draft_id="draft-2"),
    )

    batches = pack_personal_fact_review_batches(
        target_date="2026-07-15",
        candidates=[review_candidate, second_candidate],
        config=replace(
            CONFIG,
            retention_policy=replace(
                POLICY,
                fact_review_max_batch_candidates=1,
            ),
        ),
    )

    assert [len(batch.candidates) for batch in batches] == [1, 1]


def test_fact_review_prompt_states_exact_fact_item_coverage_contract() -> None:
    review_candidate = build_personal_fact_review_candidates(
        [_candidate()],
        slices=[_slice()],
        messages=_messages(),
        policy=POLICY,
    )[0]
    batch = PersonalFactReviewBatch(
        target_date="2026-07-15",
        batch_id="personal-fact-review-001",
        candidates=[review_candidate],
    )

    prompt = build_personal_fact_review_prompt(batch, config=CONFIG)
    function_spec = task_function_call_spec(
        "personal_fact_review",
        personal_fact_review_output_schema(batch),
        draft_ids=[review_candidate.candidate.draft_id],
        message_ids=review_candidate.allowed_evidence_message_ids,
        result_count=1,
    )
    output_item = function_spec.parameters["properties"]["results"]["items"]

    assert set(output_item["properties"]) == {
        "draft_id",
        "supported",
        "fact_items",
        "removed_claims",
    }
    assert output_item["required"] == [
        "draft_id",
        "supported",
        "fact_items",
        "removed_claims",
    ]
    assert output_item["additionalProperties"] is False
    assert "不要在 fact_items 之外重复返回这些文字字段" in prompt
    assert "缺少任一必填字段的合法证据时必须返回 supported=false" in prompt
    assert "Python 会直接连接所有 content.text 生成正文" in prompt
    assert "同批其他候选中出现了某个消息 ID" in prompt
