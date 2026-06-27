from __future__ import annotations

from src.worktrace.models import (
    BatchAnalysisResult,
    CrossConversationGroup,
    CrossConversationGroupResult,
    ContextRequest,
    ConversationSlice,
    NormalizedMessage,
    SourceBackedEventDraft,
)
from src.worktrace.pipeline.validation import (
    validate_batch_analysis_result,
    validate_cross_conversation_groups,
)


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


def test_validation_fills_missing_draft_id_with_stable_value() -> None:
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
                draft_id="",
                date="2026-06-22",
                topic="t",
                content="c",
                source_message_ids=["om_1"],
                source_conversation_id="oc_1",
                source_slice_id="slice-1",
                confidence=0.8,
            )
        ],
        context_requests=[],
    )

    validated = validate_batch_analysis_result(result, {"slice-1": conversation_slice})
    assert validated.candidate_events[0].draft_id == "4d0bd4f270b2a007"


def test_validation_backfills_minimal_candidate_fields_from_single_slice() -> None:
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
    result = BatchAnalysisResult.from_dict(
        {
            "candidate_events": [
                {
                    "topic": "t",
                    "content": "c",
                    "source_message_ids": ["om_1"],
                }
            ],
            "context_requests": [],
        }
    )

    validated = validate_batch_analysis_result(result, {"slice-1": conversation_slice})
    assert validated.candidate_events[0].date == "2026-06-22"
    assert validated.candidate_events[0].source_conversation_id == "oc_1"
    assert validated.candidate_events[0].source_slice_id == "slice-1"


def test_validation_uses_content_to_disambiguate_missing_draft_ids() -> None:
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
                draft_id="",
                date="2026-06-22",
                topic="t1",
                content="first content",
                source_message_ids=["om_1"],
                source_conversation_id="oc_1",
                source_slice_id="slice-1",
                confidence=0.8,
            ),
            SourceBackedEventDraft(
                draft_id="",
                date="2026-06-22",
                topic="t2",
                content="second content",
                source_message_ids=["om_1"],
                source_conversation_id="oc_1",
                source_slice_id="slice-1",
                confidence=0.8,
            ),
        ],
        context_requests=[],
    )

    validated = validate_batch_analysis_result(result, {"slice-1": conversation_slice})
    assert len(validated.candidate_events) == 2
    assert validated.candidate_events[0].draft_id != validated.candidate_events[1].draft_id


def test_validation_drops_empty_context_request() -> None:
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
        candidate_events=[],
        context_requests=[
            ContextRequest(
                slice_id="slice-1",
                request_type="later_messages",
                target_message_ids=["", "   "],
                target_attachment_ids=[],
                reason="bad request",
                limit=1,
            )
        ],
    )

    validated = validate_batch_analysis_result(result, {"slice-1": conversation_slice})
    assert validated.context_requests == []


def test_validation_backfills_minimal_context_request_from_single_slice() -> None:
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
    result = BatchAnalysisResult.from_dict(
        {
            "candidate_events": [],
            "context_requests": [
                {
                    "request_type": "later_messages",
                    "target_message_ids": ["om_1"],
                    "target_attachment_ids": [],
                }
            ],
        }
    )

    validated = validate_batch_analysis_result(result, {"slice-1": conversation_slice})
    assert len(validated.context_requests) == 1
    assert validated.context_requests[0].slice_id == "slice-1"
    assert validated.context_requests[0].limit == 1


def test_validation_deduplicates_cross_conversation_groups() -> None:
    candidates = [
        SourceBackedEventDraft(
            draft_id="d1",
            date="2026-06-22",
            topic="t1",
            content="c1",
            source_message_ids=["om_1"],
            source_conversation_id="oc_1",
            source_slice_id="slice-1",
            confidence=0.8,
        ),
        SourceBackedEventDraft(
            draft_id="d2",
            date="2026-06-22",
            topic="t2",
            content="c2",
            source_message_ids=["om_2"],
            source_conversation_id="oc_2",
            source_slice_id="slice-2",
            confidence=0.8,
        ),
    ]

    validated = validate_cross_conversation_groups(
        CrossConversationGroupResult(
            groups=[
                CrossConversationGroup(group_id="g1", draft_ids=["d1"]),
                CrossConversationGroup(group_id="g2", draft_ids=["d1"]),
                CrossConversationGroup(group_id="g3", draft_ids=["d2"]),
            ]
        ),
        candidates,
    )

    assert [group.group_id for group in validated.groups] == ["g1", "g3"]
    assert [group.draft_ids for group in validated.groups] == [["d1"], ["d2"]]
