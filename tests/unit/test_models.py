from __future__ import annotations

import pytest

from src.worktrace.constants import AnchorStatus, ContextDirection, ContextRequestType, DailyRunStatus
from src.worktrace.models import (
    AnalysisBatch,
    AnchorAnalysisResult,
    AnchorUnit,
    AttachmentMeta,
    AttachmentTextBlock,
    BatchAnalysisResult,
    CrossConversationGroup,
    CrossConversationGroupResult,
    ContextRequest,
    ConversationRef,
    ConversationSlice,
    DailyRunResult,
    DayDocument,
    EventFileLink,
    LinkMeta,
    LinkedFileTextBlock,
    MergedEventDraft,
    NormalizedMessage,
    SelfIdentity,
    SourceBackedEventDraft,
    StoreWriteResult,
    WorkEvent,
)
from src.worktrace.utils.json_io import dump_json, load_json_object


@pytest.fixture
def sample_message() -> NormalizedMessage:
    return NormalizedMessage(
        conversation_id="oc_123",
        conversation_name="项目群",
        message_id="om_001",
        sender_open_id="ou_001",
        sender_name="Alice",
        send_time="2026-06-22T09:30:00+08:00",
        message_type="text",
        text="同步今天的发布安排",
        reply_to_message_id=None,
        quote_message_id="om_000",
        links=[
            LinkMeta(
                url="https://example.feishu.cn/docx/abc",
                title="发布方案",
                link_type="feishu_doc",
            )
        ],
        attachments=[
            AttachmentMeta(
                attachment_id="att_001",
                file_name="plan.txt",
                mime_type="text/plain",
                file_size=128,
            )
        ],
        is_system=False,
    )


def test_model_roundtrip(sample_message: NormalizedMessage) -> None:
    conversation_slice = ConversationSlice(
        slice_id="slice-001",
        conversation_id="oc_123",
        conversation_name="项目群",
        anchor_message_ids=["om_001"],
        in_day_message_ids=["om_001"],
        messages=[sample_message],
        attachment_texts=[
            AttachmentTextBlock(
                attachment_id="att_001",
                message_id="om_001",
                file_name="plan.txt",
                text="发布排期补充",
            )
        ],
        linked_file_texts=[
            LinkedFileTextBlock(
                link_id="om_001#link1",
                message_id="om_001",
                title="发布方案",
                url="https://example.feishu.cn/docx/abc",
                text="方案正文",
            )
        ],
    )
    batch = AnalysisBatch(
        target_date="2026-06-22",
        batch_id="batch-001",
        retry_round=1,
        estimated_tokens=320,
        self_open_id="ou_self",
        self_display_name="Alice",
        slices=[conversation_slice],
    )
    anchor_unit = AnchorUnit(
        anchor_unit_id="oc_123:om_001",
        conversation_id="oc_123",
        conversation_name="项目群",
        anchor_message_ids=["om_001"],
        in_day_message_ids=["om_001"],
        base_message_ids=["om_001"],
        messages=[sample_message],
        reply_relation_ids=[],
        quote_relation_ids=["om_000"],
        attachment_refs=sample_message.attachments,
    )
    request = ContextRequest(
        slice_id="slice-001",
        request_type=ContextRequestType.ATTACHMENT_TEXT.value,
        target_message_ids=["om_001"],
        target_attachment_ids=["att_001"],
        target_link_ids=["om_001#link1"],
        reason="需要附件正文确认发布时间",
        limit=1,
    )
    draft = SourceBackedEventDraft(
        draft_id="draft-001",
        date="2026-06-22",
        topic="发布推进",
        content="同步发布安排并附带方案文档",
        action_label="同步",
        object_hint="发布安排",
        referenced_link_ids=["om_001#link1"],
        source_message_ids=["om_001"],
        source_conversation_id="oc_123",
        source_slice_id="slice-001",
        confidence=0.92,
    )
    batch_result = BatchAnalysisResult(
        candidate_events=[draft],
        context_requests=[request],
    )
    anchor_result = AnchorAnalysisResult(
        anchor_status=AnchorStatus.COMPLETED.value,
        candidate_events=[draft],
        context_requests=[request],
        needs_cross_anchor_merge=True,
    )
    merged = MergedEventDraft(
        date="2026-06-22",
        topic="发布推进",
        content="完成发布沟通",
        referenced_link_ids=["om_001#link1"],
        source_message_ids=["om_001"],
        source_conversation_ids=["oc_123"],
    )
    event = WorkEvent(
        date="2026-06-22",
        event_id="abcd1234abcd1234",
        title="发布推进",
        content="完成发布沟通",
        source_message_ids=["om_001"],
        referenced_link_ids=["om_001#link1"],
        file_links=[
            EventFileLink(
                url="https://example.feishu.cn/docx/abc",
                title="发布方案",
                link_type="feishu_doc",
            )
        ],
    )
    group = CrossConversationGroup(group_id="g1", draft_ids=["draft-001"])
    group_result = CrossConversationGroupResult(groups=[group])
    day_doc = DayDocument(
        date="2026-06-22",
        events=[event],
        generated_at="2026-06-22T20:00:00+08:00",
    )
    store_result = StoreWriteResult(
        output_path="/tmp/2026-06-22.md",
        event_count=1,
        written_at="2026-06-22T20:01:00+08:00",
    )
    run_result = DailyRunResult(
        target_date="2026-06-22",
        conversation_count=1,
        message_count=4,
        slice_count=1,
        batch_count=1,
        event_count=1,
        skipped_slice_count=0,
        warning_count=0,
        status=DailyRunStatus.SUCCESS.value,
        output_path="/tmp/2026-06-22.md",
        error_summary="",
        self_delivery_status="pending",
        self_delivery_target="",
        self_delivery_error="",
    )

    payloads = [
        SelfIdentity(open_id="ou_001", display_name="Alice", source="lark-cli"),
        ConversationRef(conversation_id="oc_123", conversation_name="项目群"),
        sample_message.links[0],
        sample_message.attachments[0],
        sample_message,
        conversation_slice.attachment_texts[0],
        conversation_slice.linked_file_texts[0],
        conversation_slice,
        batch,
        anchor_unit,
        request,
        draft,
        batch_result,
        anchor_result,
        merged,
        group,
        group_result,
        event,
        day_doc,
        store_result,
        run_result,
    ]

    for payload in payloads:
        payload_type = type(payload)
        assert payload_type.from_dict(payload.to_dict()) == payload


def test_json_helpers_roundtrip() -> None:
    raw = {"target_date": "2026-06-22", "status": "success"}
    text = dump_json(raw)

    assert load_json_object(text) == raw


def test_event_models_default_referenced_link_ids_are_empty() -> None:
    draft = SourceBackedEventDraft.from_dict(
        {
            "draft_id": "draft-001",
            "date": "2026-06-22",
            "topic": "发布推进",
            "content": "完成发布沟通",
            "source_message_ids": ["om_1"],
            "source_conversation_id": "oc_1",
            "source_slice_id": "slice-1",
            "confidence": 0.9,
        }
    )
    merged = MergedEventDraft.from_dict(
        {
            "date": "2026-06-22",
            "topic": "发布推进",
            "content": "完成发布沟通",
            "source_message_ids": ["om_1"],
            "source_conversation_ids": ["oc_1"],
        }
    )
    event = WorkEvent.from_dict(
        {
            "date": "2026-06-22",
            "event_id": "evt1",
            "title": "发布推进",
            "content": "完成发布沟通",
            "source_message_ids": ["om_1"],
        }
    )

    assert draft.referenced_link_ids == []
    assert merged.referenced_link_ids == []
    assert event.referenced_link_ids == []
    assert draft.self_relations == []
    assert merged.workstream_name == ""
    assert merged.action_labels == []
    assert merged.self_relations == []
    assert event.workstream_name == ""
    assert event.action_labels == []
    assert event.self_relations == []
    assert event.evidence_fingerprints == []
    assert event.conversation_fingerprints == []
    assert event.file_keys == []


def test_constants_are_string_enums() -> None:
    assert DailyRunStatus.SUCCESS.value == "success"
    assert ContextRequestType.EARLIER_MESSAGES.value == "earlier_messages"
    assert ContextDirection.LATER.value == "later"
    assert AnchorStatus.COMPLETED.value == "completed"
