from __future__ import annotations

from src.worktrace.models import (
    BatchAnalysisResult,
    ContextRequest,
    ConversationSlice,
    NormalizedMessage,
    SourceBackedEventDraft,
)
from src.worktrace.pipeline.validation import validate_batch_analysis_result


def test_validation_normalizes_source_ids() -> None:
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_self",
        sender_name="Me",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text="hello",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    conversation_slice = ConversationSlice(
        slice_id="slice-1",
        conversation_id="oc_1",
        conversation_name="项目群",
        anchor_message_ids=["om_1"],
        in_day_message_ids=["om_1"],
        messages=[message],
        attachment_texts=[],
    )
    result = BatchAnalysisResult(
        candidate_events=[
            SourceBackedEventDraft(
                draft_id="d1",
                date="2026-06-22",
                topic="t",
                content="c",
                result="r",
                source_message_ids=["bad", "om_1", "om_1"],
                source_conversation_id="oc_1",
                source_slice_id="slice-1",
                confidence=0.8,
            )
        ],
        context_requests=[ContextRequest(slice_id="slice-1", request_type="later_messages", target_message_ids=["om_1"], target_attachment_ids=[], reason="", limit=1)],
    )

    validated = validate_batch_analysis_result(result, {"slice-1": conversation_slice})
    assert validated.candidate_events[0].source_message_ids == ["om_1"]
    assert len(validated.context_requests) == 1


def test_validation_maps_prompt_short_ids_back_to_message_ids() -> None:
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_self",
        sender_name="Me",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text="hello",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    conversation_slice = ConversationSlice(
        slice_id="slice-1",
        conversation_id="oc_1",
        conversation_name="项目群",
        anchor_message_ids=["om_1"],
        in_day_message_ids=["om_1"],
        messages=[message],
        attachment_texts=[],
    )
    result = BatchAnalysisResult(
        candidate_events=[
            SourceBackedEventDraft(
                draft_id="d1",
                date="2026-06-22",
                topic="t",
                content="c",
                result="r",
                source_message_ids=["m1"],
                source_conversation_id="oc_1",
                source_slice_id="slice-1",
                confidence=0.8,
            )
        ],
        context_requests=[],
    )

    validated = validate_batch_analysis_result(result, {"slice-1": conversation_slice})
    assert validated.candidate_events[0].source_message_ids == ["om_1"]
