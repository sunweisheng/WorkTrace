from __future__ import annotations

import json

from src.worktrace.analyzers.prompts import (
    build_conversation_segmentation_prompt,
    build_segment_batch_analysis_prompt,
    restore_conversation_segmentation_references,
)
from src.worktrace.analyzers.output_schemas import conversation_segmentation_output_schema
from src.worktrace.config import RuntimeConfig
from src.worktrace.models import (
    BatchAnalysisResult,
    BatchSegmentAnalysisItem,
    BatchSegmentAnalysisResult,
    ConversationSegment,
    ConversationSegmentationResult,
    ConversationSegmentUnit,
    NormalizedMessage,
    ResponseSignal,
    SegmentAnalysisBatch,
    SourceBackedEventDraft,
)
from src.worktrace.pipeline.conversation_segments import (
    pack_segment_units,
    validate_conversation_segmentation,
    validate_segment_batch_result,
)


def _message(
    message_id: str,
    *,
    sender_open_id: str,
    minute: int,
    text: str = "消息",
    reply_to_message_id: str | None = None,
    mentioned_open_ids: list[str] | None = None,
) -> NormalizedMessage:
    return NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id=message_id,
        sender_open_id=sender_open_id,
        sender_name=sender_open_id,
        send_time=f"2026-07-10T09:{minute:02d}:00+08:00",
        message_type="text",
        text=text,
        reply_to_message_id=reply_to_message_id,
        quote_message_id=None,
        mentioned_open_ids=mentioned_open_ids or [],
    )


def _candidate(
    source_message_ids: list[str],
    *,
    response_outcome: str = "unknown",
    response_signal_ids: list[str] | None = None,
    response_evidence_message_ids: list[str] | None = None,
) -> SourceBackedEventDraft:
    return SourceBackedEventDraft(
        draft_id="draft",
        date="2026-07-10",
        topic="项目事项",
        content="确认项目事项的处理结论。",
        source_message_ids=source_message_ids,
        source_conversation_id="oc_1",
        source_slice_id="turn",
        confidence=0.9,
        action_label="确认",
        object_hint="项目事项",
        retention_reason="decision_made",
        retention_detail="沟通中确认了项目事项的处理结论。",
        response_outcome=response_outcome,
        response_signal_ids=response_signal_ids or [],
        response_evidence_message_ids=response_evidence_message_ids or [],
    )


def _unit(
    segment_id: str,
    messages: list[NormalizedMessage],
    *,
    primary_message_ids: list[str],
    context_message_ids: list[str] | None = None,
    self_evidence_message_ids: list[str] | None = None,
    response_signals: list[ResponseSignal] | None = None,
    response_assessments: list[ResponseAssessment] | None = None,
) -> ConversationSegmentUnit:
    return ConversationSegmentUnit(
        segment_id=segment_id,
        conversation_id="oc_1",
        conversation_name="项目群",
        primary_message_ids=primary_message_ids,
        context_message_ids=context_message_ids or [],
        self_evidence_message_ids=self_evidence_message_ids or [],
        response_signals=response_signals or [],
        response_assessments=response_assessments or [],
        messages=messages,
    )


def test_segmentation_keeps_other_recipient_after_self_reply_out_of_self_units() -> None:
    messages = [
        _message("om_1", sender_open_id="ou_ding", minute=0, text="请跟进发布"),
        _message("om_2", sender_open_id="ou_self", minute=1, text="收到"),
        _message(
            "om_3",
            sender_open_id="ou_ding",
            minute=2,
            text="请张玉环约会议",
            mentioned_open_ids=["ou_yuhuan"],
        ),
        _message("om_4", sender_open_id="ou_yuhuan", minute=3, text="我来安排"),
    ]
    result = ConversationSegmentationResult(
        segments=[
            ConversationSegment(
                segment_id="turn-zhang-baohua",
                primary_message_ids=["om_1", "om_2"],
                self_evidence_message_ids=["om_2"],
            ),
            ConversationSegment(
                segment_id="turn-zhang-yuhuan",
                primary_message_ids=["om_3", "om_4"],
            ),
        ]
    )

    units, warnings = validate_conversation_segmentation(
        result,
        messages,
        self_open_id="ou_self",
        self_display_name="张宝华",
        self_assignment_keywords=(),
        response_signals=[],
    )

    assert warnings == []
    assert [unit.segment_id for unit in units] == ["turn-zhang-baohua"]
    assert units[0].primary_message_ids == ["om_1", "om_2"]


def test_segmentation_derives_self_evidence_when_the_model_leaves_it_empty() -> None:
    messages = [
        _message("om_1", sender_open_id="ou_other", minute=0, text="请推进发布"),
        _message("om_2", sender_open_id="ou_self", minute=1, text="收到，我来跟进"),
    ]
    result = ConversationSegmentationResult(
        segments=[
            ConversationSegment(
                segment_id="turn-1",
                primary_message_ids=["om_1", "om_2"],
            )
        ]
    )

    units, warnings = validate_conversation_segmentation(
        result,
        messages,
        self_open_id="ou_self",
        self_display_name="张宝华",
        self_assignment_keywords=(),
        response_signals=[],
    )

    assert warnings == []
    assert units[0].self_evidence_message_ids == ["om_2"]


def test_segmentation_schema_requires_a_nonempty_partition() -> None:
    schema = conversation_segmentation_output_schema()
    start_schema = schema["properties"]["segment_start_message_ids"]

    assert schema["required"] == ["segment_start_message_ids"]
    assert start_schema["minItems"] == 1


def test_segmentation_prompt_uses_short_references_and_restores_them() -> None:
    messages = [
        _message("opaque-message-id-1", sender_open_id="ou_other", minute=0),
        _message("opaque-message-id-2", sender_open_id="ou_self", minute=1),
    ]
    signal = ResponseSignal(
        signal_id="text:opaque-message-id-2",
        kind="text",
        message_id="opaque-message-id-2",
        action_time=messages[1].send_time,
    )
    prompt = build_conversation_segmentation_prompt(
        target_date="2026-07-10",
        conversation_id="oc_1",
        conversation_name="项目群",
        messages=messages,
        self_open_id="ou_self",
        self_display_name="张宝华",
        response_signals=[signal],
        hard_boundary_before_ids=set(),
    )

    payload = json.loads(prompt)
    assert payload["input"]["message_refs_in_order"] == ["m001", "m002"]
    assert [item["id"] for item in payload["input"]["messages"]] == ["m001", "m002"]
    assert [item["sent_by_self"] for item in payload["input"]["messages"]] == [False, True]
    assert payload["input"]["response_signals"][0]["signal_id"] == "r001"
    assert "opaque-message-id-1" not in prompt
    assert "opaque-message-id-2" not in prompt

    restored = restore_conversation_segmentation_references(
        ConversationSegmentationResult(
            segment_start_message_ids=["m001"],
        ),
        messages=messages,
        response_signals=[signal],
    )

    assert restored.segment_start_message_ids == ["opaque-message-id-1"]
    assert restored.segments == []


def test_segmentation_start_ids_preserve_the_immutable_message_timeline() -> None:
    messages = [
        _message("om_1", sender_open_id="ou_self", minute=0),
        _message("om_2", sender_open_id="ou_other", minute=1),
        _message("om_3", sender_open_id="ou_self", minute=2),
        _message("om_4", sender_open_id="ou_other", minute=3),
    ]
    result = ConversationSegmentationResult(
        segment_start_message_ids=["om_1", "om_3"],
    )

    units, warnings = validate_conversation_segmentation(
        result,
        messages,
        self_open_id="ou_self",
        self_display_name="张宝华",
        self_assignment_keywords=(),
        response_signals=[],
    )

    assert warnings == []
    assert [item.primary_message_ids for item in units] == [
        ["om_1", "om_2"],
        ["om_3", "om_4"],
    ]


def test_segmentation_start_ids_reject_reordered_starts() -> None:
    messages = [
        _message("om_1", sender_open_id="ou_self", minute=0),
        _message("om_2", sender_open_id="ou_other", minute=1, mentioned_open_ids=["ou_other"]),
        _message("om_3", sender_open_id="ou_self", minute=2),
    ]
    result = ConversationSegmentationResult(
        segment_start_message_ids=["om_1", "om_3", "om_2"],
    )

    units, warnings = validate_conversation_segmentation(
        result,
        messages,
        self_open_id="ou_self",
        self_display_name="张宝华",
        self_assignment_keywords=(),
        response_signals=[],
    )

    assert units == []
    assert warnings == [
        "Skipped conversation because segmentation start messages were not ordered."
    ]


def test_segmentation_start_ids_require_recipient_boundaries() -> None:
    messages = [
        _message("om_1", sender_open_id="ou_self", minute=0),
        _message("om_2", sender_open_id="ou_other", minute=1, mentioned_open_ids=["ou_other"]),
        _message("om_3", sender_open_id="ou_self", minute=2),
    ]
    result = ConversationSegmentationResult(segment_start_message_ids=["om_1"])

    units, warnings = validate_conversation_segmentation(
        result,
        messages,
        self_open_id="ou_self",
        self_display_name="张宝华",
        self_assignment_keywords=(),
        response_signals=[],
    )

    assert units == []
    assert warnings == [
        "Skipped conversation because segmentation omitted a recipient boundary."
    ]


def test_segment_prompt_recombines_context_and_primary_messages_in_time_order() -> None:
    old_message = _message("om_1", sender_open_id="ou_self", minute=0, text="旧事项结论")
    continuation = _message(
        "om_2",
        sender_open_id="ou_other",
        minute=5,
        text="引用旧事项继续讨论新动作",
        reply_to_message_id="om_1",
    )
    unit = _unit(
        "turn-new",
        [old_message, continuation],
        primary_message_ids=["om_2"],
        context_message_ids=["om_1"],
        self_evidence_message_ids=["om_2"],
    )
    batch = SegmentAnalysisBatch(
        target_date="2026-07-10",
        conversation_id="oc_1",
        conversation_name="项目群",
        self_open_id="ou_self",
        self_display_name="张宝华",
        segments=[unit],
    )

    payload = json.loads(build_segment_batch_analysis_prompt(batch))
    prompt_messages = payload["input"]["segments"][0]["messages"]

    assert [item["id"] for item in prompt_messages] == ["om_1", "om_2"]
    assert [item["role"] for item in prompt_messages] == ["context", "primary"]


def test_segment_batch_packing_preserves_validated_turn_order() -> None:
    first = _unit(
        "turn-later-id",
        [_message("om_9", sender_open_id="ou_self", minute=0)],
        primary_message_ids=["om_9"],
        self_evidence_message_ids=["om_9"],
    )
    second = _unit(
        "turn-earlier-id",
        [_message("om_1", sender_open_id="ou_self", minute=1)],
        primary_message_ids=["om_1"],
        self_evidence_message_ids=["om_1"],
    )

    batches = pack_segment_units(
        target_date="2026-07-10",
        self_open_id="ou_self",
        self_display_name="张宝华",
        units=[first, second],
        config=RuntimeConfig(max_model_input_tokens=100_000),
    )

    assert [unit.segment_id for unit in batches[0].segments] == [
        "turn-later-id",
        "turn-earlier-id",
    ]


def test_segment_batch_filters_duplicate_unknown_and_cross_segment_results() -> None:
    first_message = _message("om_1", sender_open_id="ou_self", minute=0)
    second_message = _message("om_2", sender_open_id="ou_self", minute=1)
    first = _unit(
        "turn-1",
        [first_message],
        primary_message_ids=["om_1"],
        self_evidence_message_ids=["om_1"],
    )
    second = _unit(
        "turn-2",
        [second_message],
        primary_message_ids=["om_2"],
        self_evidence_message_ids=["om_2"],
    )
    batch = SegmentAnalysisBatch(
        target_date="2026-07-10",
        conversation_id="oc_1",
        conversation_name="项目群",
        self_open_id="ou_self",
        self_display_name="张宝华",
        segments=[first, second],
    )
    result = BatchSegmentAnalysisResult(
        results=[
            BatchSegmentAnalysisItem("turn-1", BatchAnalysisResult()),
            BatchSegmentAnalysisItem("turn-1", BatchAnalysisResult()),
            BatchSegmentAnalysisItem("unknown", BatchAnalysisResult()),
            BatchSegmentAnalysisItem(
                "turn-2",
                BatchAnalysisResult(candidate_events=[_candidate(["om_1"])]),
            ),
        ]
    )

    valid, missing, warnings = validate_segment_batch_result(result, batch)

    assert "turn-1" not in valid
    assert valid["turn-2"].candidate_events == []
    assert [unit.segment_id for unit in missing] == ["turn-1"]
    assert any("invalid segment batch" in item for item in warnings)
    assert any("cross-segment source" in item for item in warnings)


def test_segment_batch_does_not_require_a_response_assessment() -> None:
    first = _message("om_1", sender_open_id="ou_self", minute=0, text="收到")
    second = _message("om_2", sender_open_id="ou_other", minute=1, text="后续继续推进")
    signal = ResponseSignal(
        signal_id="text:om_1",
        kind="text",
        message_id="om_1",
        action_time=first.send_time,
    )
    unit = _unit(
        "turn-1",
        [first, second],
        primary_message_ids=["om_1", "om_2"],
        self_evidence_message_ids=["om_1"],
        response_signals=[signal],
    )
    batch = SegmentAnalysisBatch(
        target_date="2026-07-10",
        conversation_id="oc_1",
        conversation_name="项目群",
        self_open_id="ou_self",
        self_display_name="张宝华",
        segments=[unit],
    )
    result = BatchSegmentAnalysisResult(
        results=[
            BatchSegmentAnalysisItem(
                "turn-1",
                BatchAnalysisResult(
                    candidate_events=[
                        _candidate(
                            ["om_1", "om_2"],
                            response_outcome="accepted",
                            response_signal_ids=["text:om_1"],
                            response_evidence_message_ids=["om_2"],
                        )
                    ]
                ),
            )
        ]
    )

    valid, missing, warnings = validate_segment_batch_result(result, batch)

    assert missing == []
    assert len(valid["turn-1"].candidate_events) == 1
    assert warnings == []
