from __future__ import annotations

from src.worktrace.models import (
    AttachmentMeta,
    BatchAnalysisResult,
    CrossConversationGroup,
    CrossConversationGroupResult,
    ContextRequest,
    ConversationSlice,
    LinkMeta,
    NormalizedMessage,
    SelfRelationEvidence,
    SourceBackedEventDraft,
)
from src.worktrace.pipeline.validation import (
    normalize_cross_conversation_groups_with_fallback,
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


def test_validation_keeps_source_message_attachment_without_reading_its_body() -> None:
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_self",
        sender_name="Me",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="file",
        text="[文件附件]",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[
            AttachmentMeta(
                attachment_id="att_1",
                file_name="收款公司清单.xlsx",
                mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                file_size=1,
            )
        ],
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
                topic="文件审核",
                content="发送文件审核",
                source_message_ids=["om_1"],
                source_conversation_id="oc_1",
                source_slice_id="slice-1",
                referenced_attachment_ids=["att_1"],
                confidence=0.8,
            )
        ],
        context_requests=[],
    )

    validated = validate_batch_analysis_result(result, {"slice-1": conversation_slice})

    assert validated.candidate_events[0].referenced_attachment_ids == ["att_1"]


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
    assert validated.candidate_events[0].draft_id == "b76138cfa97c4719"


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

    validated = validate_batch_analysis_result(
        result,
        {"slice-1": conversation_slice},
        self_open_id="ou_self",
    )
    assert len(validated.candidate_events) == 2
    assert validated.candidate_events[0].draft_id == "b76138cfa97c4719"
    assert validated.candidate_events[1].draft_id == "b76138cfa97c4719-d7d49cf0"


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


def test_validation_accepts_linked_file_text_request_with_known_link_id() -> None:
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_self",
        sender_name="Me",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text="请看 https://foo.feishu.cn/docx/abc",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[
            LinkMeta(
                url="https://foo.feishu.cn/docx/abc",
                title="方案",
                link_type="feishu_doc",
            )
        ],
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
    )
    result = BatchAnalysisResult(
        candidate_events=[],
        context_requests=[
            ContextRequest(
                slice_id="slice-1",
                request_type="linked_file_text",
                target_message_ids=["om_1"],
                target_link_ids=["om_1#link1"],
                reason="需要文档正文",
                limit=1,
            )
        ],
    )

    validated = validate_batch_analysis_result(result, {"slice-1": conversation_slice})

    assert len(validated.context_requests) == 1
    assert validated.context_requests[0].target_link_ids == ["om_1#link1"]


def test_validation_drops_linked_file_text_request_with_unknown_link_id() -> None:
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
    )
    result = BatchAnalysisResult(
        candidate_events=[],
        context_requests=[
            ContextRequest(
                slice_id="slice-1",
                request_type="linked_file_text",
                target_message_ids=["om_1"],
                target_link_ids=["om_1#link1"],
                reason="需要文档正文",
                limit=1,
            )
        ],
    )

    validated = validate_batch_analysis_result(result, {"slice-1": conversation_slice})

    assert validated.context_requests == []


def test_validation_keeps_valid_self_relations_and_warns_for_invalid_items() -> None:
    self_message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_self",
        sender_open_id="ou_self",
        sender_name="Me",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text="我来发起并推进",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    other_message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_other",
        sender_open_id="ou_other",
        sender_name="Other",
        send_time="2026-06-22T10:01:00+08:00",
        message_type="text",
        text="收到",
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
        anchor_message_ids=["om_self"],
        in_day_message_ids=["om_self"],
        messages=[self_message, other_message],
    )
    result = BatchAnalysisResult(
        candidate_events=[
            SourceBackedEventDraft(
                draft_id="d1",
                date="2026-06-22",
                topic="项目发起",
                content="发起项目并推进。",
                source_message_ids=["om_self"],
                source_conversation_id="oc_1",
                source_slice_id="slice-1",
                confidence=0.9,
                self_evidence_message_ids=["om_self", "om_other"],
                self_relations=[
                    SelfRelationEvidence("initiated", ["om_self"]),
                    SelfRelationEvidence("unknown", ["om_self"]),
                    SelfRelationEvidence("collaboration", ["om_other"]),
                    SelfRelationEvidence("primary_execution", ["om_outside"]),
                ],
            )
        ]
    )
    warnings: list[str] = []

    validated = validate_batch_analysis_result(
        result,
        {"slice-1": conversation_slice},
        self_open_id="ou_self",
        self_relation_keys=("initiated", "primary_execution", "collaboration"),
        warning_sink=warnings,
    )

    assert validated.candidate_events[0].self_relations == [
        SelfRelationEvidence("initiated", ["om_self"])
    ]
    assert len(warnings) == 3


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


def test_validation_repairs_missing_cross_conversation_groups() -> None:
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

    repaired, warnings = normalize_cross_conversation_groups_with_fallback(
        CrossConversationGroupResult(
            groups=[
                CrossConversationGroup(group_id="g1", draft_ids=["d1"]),
            ]
        ),
        candidates,
    )

    assert [group.group_id for group in repaired.groups] == ["g1", "fallback-1"]
    assert [group.draft_ids for group in repaired.groups] == [["d1"], ["d2"]]
    assert warnings == [
        "Cross-conversation merge groups were repaired: missing=['d2']"
    ]
