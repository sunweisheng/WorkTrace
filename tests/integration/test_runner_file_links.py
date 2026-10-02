from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.worktrace.config import EventMetadataItem, RuntimeConfig
from src.worktrace.constants import DailyRunStatus
from src.worktrace.factories import RuntimeDependencies
from src.worktrace.models import (
    AttachmentMeta,
    AnchorAnalysisResult,
    BatchAnchorAnalysisItem,
    BatchAnchorAnalysisResult,
    BatchAnalysisResult,
    BatchSegmentAnalysisItem,
    BatchSegmentAnalysisResult,
    ConversationRef,
    ContextRequest,
    ConversationSegment,
    ConversationSegmentationResult,
    CrossConversationGroup,
    CrossConversationGroupResult,
    EventFileLink,
    LinkMeta,
    NormalizedMessage,
    SelfIdentity,
    SelfRelationEvidence,
    SourceBackedEventDraft,
    WorkEvent,
)
from src.worktrace.runner import DailyTraceRunner, _attach_event_file_links
from src.worktrace.resolvers.feishu_message import FeishuMessageContentResolver
from src.worktrace.stores.markdown import MarkdownEventStore
from tests.helpers import FunctionRequestStub


class LinkSource:
    def get_self_identity(self):
        return SelfIdentity(open_id="ou_self", display_name="Me", source="fake")

    def list_target_conversations(self, target_date, self_identity):
        return [ConversationRef(conversation_id="oc_1", conversation_name="项目群")]

    def fetch_conversation_messages(self, target_date, conversation_ids):
        return [
            NormalizedMessage(
                conversation_id="oc_1",
                conversation_name="项目群",
                message_id="om_1",
                sender_open_id="ou_self",
                sender_name="Me",
                send_time="2026-06-22T10:00:00+08:00",
                message_type="text",
                text="请看文档 https://foo.feishu.cn/docx/abc",
                reply_to_message_id=None,
                quote_message_id=None,
                links=[
                    LinkMeta(
                        url="https://foo.feishu.cn/docx/abc",
                        title="发布方案",
                        link_type="feishu_doc",
                    )
                ],
                attachments=[],
                is_system=False,
            )
        ]

    def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
        return []


class LinkResolver:
    def to_text(self, message):
        return message.text

    def extract_links(self, message):
        return list(message.links)

    def load_attachment_text_if_needed(self, message, attachment_ids, hint):
        return None


class LinkAnalyzer(FunctionRequestStub):
    def build_batch_prompt(self, batch_input):
        return "prompt"

    def analyze_batch(self, target_date, batch_input):
        return BatchAnalysisResult(
            candidate_events=[
                SourceBackedEventDraft(
                    draft_id="draft-1",
                    date="2026-06-22",
                    topic="发布推进",
                    content="完成发布沟通",
                    source_message_ids=["om_1"],
                    source_conversation_id="oc_1",
                    source_slice_id=batch_input.slices[0].slice_id,
                    confidence=0.9,
                    action_label="确认",
                    object_hint="发布方案",
                    retention_reason="deliverable_updated",
                    retention_detail="确认发布方案文档中的发布推进信息。",
                    referenced_link_ids=["om_1#link1"],
                )
            ],
            context_requests=[],
        )

    def merge_day_candidates(self, target_date, candidates, *, validation_feedback=""):
        raise AssertionError("Should not group when there is only one candidate")


class LinkDelivery:
    def deliver_to_self(self, *, self_identity, markdown_path):
        return ("success", self_identity.open_id)


def test_runner_attaches_file_links_from_source_messages(tmp_path: Path) -> None:
    config = RuntimeConfig(data_root=tmp_path / "data")
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=LinkSource(),
            content_resolver=LinkResolver(),
            analyzer=LinkAnalyzer(),
            delivery_channel=LinkDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert result.output_path is not None

    content = Path(result.output_path).read_text(encoding="utf-8")
    assert "[发布方案](https://foo.feishu.cn/docx/abc)" in content


def test_runner_prefers_named_file_link_when_same_url_appears_multiple_times(tmp_path: Path) -> None:
    resolver = FeishuMessageContentResolver(config=RuntimeConfig(data_root=tmp_path / "data"))
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_self",
        sender_name="Me",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text="https://foo.feishu.cn/docx/abc",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[
            LinkMeta(
                url="https://foo.feishu.cn/docx/abc",
                title="发布方案",
                link_type="feishu_doc",
            )
        ],
        attachments=[],
        is_system=False,
    )
    event = WorkEvent(
        date="2026-06-22",
        event_id="evt1",
        title="发布推进",
        content="完成发布沟通",
        source_message_ids=["om_1"],
        referenced_link_ids=["om_1#link1"],
        file_links=[],
        object_hint="发布方案",
        retention_reason="deliverable_updated",
        retention_detail="确认发布方案文档中的发布推进信息。",
    )

    attached = __import__("src.worktrace.runner", fromlist=["_attach_event_file_links"])._attach_event_file_links(
        [event],
        messages=[message],
        content_resolver=resolver,
    )

    assert attached[0].file_links == [
        EventFileLink(
            url="https://foo.feishu.cn/docx/abc",
            title="发布方案",
            link_type="feishu_doc",
        )
    ]
    assert attached[0].retention_reason == "deliverable_updated"


def test_runner_attaches_only_llm_selected_link(tmp_path: Path) -> None:
    resolver = FeishuMessageContentResolver(config=RuntimeConfig(data_root=tmp_path / "data"))
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_self",
        sender_name="Me",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text="方案A https://foo.feishu.cn/docx/abc 方案B https://foo.feishu.cn/docx/def",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[
            LinkMeta(
                url="https://foo.feishu.cn/docx/abc",
                title="方案A",
                link_type="feishu_doc",
            ),
            LinkMeta(
                url="https://foo.feishu.cn/docx/def",
                title="方案B",
                link_type="feishu_doc",
            ),
        ],
        attachments=[],
        is_system=False,
    )
    event = WorkEvent(
        date="2026-06-22",
        event_id="evt1",
        title="发布推进",
        content="完成发布沟通",
        source_message_ids=["om_1"],
        referenced_link_ids=["om_1#link2"],
        file_links=[],
        object_hint="发布方案",
        retention_reason="deliverable_updated",
        retention_detail="确认发布方案文档中的发布推进信息。",
    )

    attached = __import__("src.worktrace.runner", fromlist=["_attach_event_file_links"])._attach_event_file_links(
        [event],
        messages=[message],
        content_resolver=resolver,
    )

    assert attached[0].file_links == [
        EventFileLink(
            url="https://foo.feishu.cn/docx/def",
            title="方案B",
            link_type="feishu_doc",
        )
    ]


def test_runner_keeps_selected_bare_url_when_event_text_supports_it(tmp_path: Path) -> None:
    resolver = FeishuMessageContentResolver(config=RuntimeConfig(data_root=tmp_path / "data"))
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_self",
        sender_name="Me",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text="https://github.com/sunweisheng/WorkTrace",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    event = WorkEvent(
        date="2026-06-22",
        event_id="evt1",
        title="WorkTrace v1.0.5 发布",
        content="发布 WorkTrace 新版本。",
        source_message_ids=["om_1"],
        referenced_link_ids=["om_1#link1"],
        file_links=[],
        object_hint="WorkTrace 发布",
        retention_reason="deliverable_updated",
        retention_detail="同步 WorkTrace 新版本发布。",
    )

    attached = __import__("src.worktrace.runner", fromlist=["_attach_event_file_links"])._attach_event_file_links(
        [event],
        messages=[message],
        content_resolver=resolver,
    )

    assert attached[0].file_links == [
        EventFileLink(
            url="https://github.com/sunweisheng/WorkTrace",
            title="",
            link_type="normal",
        )
    ]


def test_runner_drops_nonexistent_referenced_link_ids(tmp_path: Path) -> None:
    class InvalidLinkAnalyzer(LinkAnalyzer):
        def analyze_batch(self, target_date, batch_input):
            return BatchAnalysisResult(
                candidate_events=[
                    SourceBackedEventDraft(
                        draft_id="draft-1",
                        date="2026-06-22",
                        topic="发布推进",
                        content="完成发布沟通",
                        source_message_ids=["om_1"],
                        source_conversation_id="oc_1",
                        source_slice_id=batch_input.slices[0].slice_id,
                        confidence=0.9,
                        action_label="确认",
                        object_hint="发布方案",
                        retention_reason="deliverable_updated",
                        retention_detail="确认发布方案文档中的发布推进信息。",
                        referenced_link_ids=["om_1#link9"],
                    )
                ],
                context_requests=[],
            )

    config = RuntimeConfig(data_root=tmp_path / "data")
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=LinkSource(),
            content_resolver=LinkResolver(),
            analyzer=InvalidLinkAnalyzer(),
            delivery_channel=LinkDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert result.output_path is not None
    content = Path(result.output_path).read_text(encoding="utf-8")
    assert "[发布方案](https://foo.feishu.cn/docx/abc)" not in content
    assert "  - 无" in content


def test_runner_drops_referenced_links_outside_source_message_ids(tmp_path: Path) -> None:
    class MultiMessageSource(LinkSource):
        def fetch_conversation_messages(self, target_date, conversation_ids):
            return [
                NormalizedMessage(
                    conversation_id="oc_1",
                    conversation_name="项目群",
                    message_id="om_1",
                    sender_open_id="ou_self",
                    sender_name="Me",
                    send_time="2026-06-22T10:00:00+08:00",
                    message_type="text",
                    text="推进发布",
                    reply_to_message_id=None,
                    quote_message_id=None,
                    links=[],
                    attachments=[],
                    is_system=False,
                ),
                NormalizedMessage(
                    conversation_id="oc_1",
                    conversation_name="项目群",
                    message_id="om_2",
                    sender_open_id="ou_other",
                    sender_name="Alice",
                    send_time="2026-06-22T10:01:00+08:00",
                    message_type="text",
                    text="请看文档 https://foo.feishu.cn/docx/abc",
                    reply_to_message_id=None,
                    quote_message_id=None,
                    links=[
                        LinkMeta(
                            url="https://foo.feishu.cn/docx/abc",
                            title="发布方案",
                            link_type="feishu_doc",
                        )
                    ],
                    attachments=[],
                    is_system=False,
                ),
            ]

    class CrossMessageLinkAnalyzer(LinkAnalyzer):
        def analyze_batch(self, target_date, batch_input):
            return BatchAnalysisResult(
                candidate_events=[
                    SourceBackedEventDraft(
                        draft_id="draft-1",
                        date="2026-06-22",
                        topic="发布推进",
                        content="完成发布沟通",
                        source_message_ids=["om_1"],
                        source_conversation_id="oc_1",
                        source_slice_id=batch_input.slices[0].slice_id,
                        confidence=0.9,
                        action_label="确认",
                        object_hint="发布方案",
                        retention_reason="deliverable_updated",
                        retention_detail="确认发布推进安排。",
                        referenced_link_ids=["om_2#link1"],
                    )
                ],
                context_requests=[],
            )

    config = RuntimeConfig(data_root=tmp_path / "data")
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=MultiMessageSource(),
            content_resolver=LinkResolver(),
            analyzer=CrossMessageLinkAnalyzer(),
            delivery_channel=LinkDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert result.output_path is not None
    content = Path(result.output_path).read_text(encoding="utf-8")
    assert "[发布方案](https://foo.feishu.cn/docx/abc)" not in content
    assert "  - 无" in content


def test_runner_keeps_selected_link_without_event_text_support(tmp_path: Path) -> None:
    resolver = FeishuMessageContentResolver(config=RuntimeConfig(data_root=tmp_path / "data"))
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_self",
        sender_name="Me",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text="https://skills.gydev.cn/space/global/worktrace",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    event = WorkEvent(
        date="2026-06-22",
        event_id="evt1",
        title="哈尔滨项目协议签署情况同步",
        content="同步哈尔滨项目协议签署和法务安排。",
        source_message_ids=["om_1"],
        referenced_link_ids=["om_1#link1"],
        file_links=[],
        object_hint="哈尔滨项目协议",
        retention_reason="decision_made",
        retention_detail="同步哈尔滨项目协议沟通结论。",
    )

    attached = __import__("src.worktrace.runner", fromlist=["_attach_event_file_links"])._attach_event_file_links(
        [event],
        messages=[message],
        content_resolver=resolver,
    )

    assert [link.url for link in attached[0].file_links] == [
        "https://skills.gydev.cn/space/global/worktrace"
    ]


def test_runner_makes_doc_token_references_readable_in_event_text(tmp_path: Path) -> None:
    resolver = FeishuMessageContentResolver(config=RuntimeConfig(data_root=tmp_path / "data"))
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_other",
        sender_name="Alice",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text="请看文档 https://foo.feishu.cn/wiki/MgYnwgMIkiUDGGkjHYLcEMaEnhd",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[
            LinkMeta(
                url="https://foo.feishu.cn/wiki/MgYnwgMIkiUDGGkjHYLcEMaEnhd",
                title="仓库摄像头录制 PRD",
                link_type="feishu_doc",
            )
        ],
        attachments=[],
        is_system=False,
    )
    event = WorkEvent(
        date="2026-06-22",
        event_id="evt1",
        title="文档优先级定级为P2",
        content="针对飞书文档（MgYnwgMIkiUDGGkjHYLcEMaEnhd），本人审阅后将其优先级标记为 P2。",
        source_message_ids=["om_1"],
        referenced_link_ids=[],
        file_links=[],
        object_hint="飞书文档优先级定级",
        retention_reason="substantive_approval",
        retention_detail="本人回复时明确给出了“优先级P2”的处理结论。",
    )

    attached = __import__("src.worktrace.runner", fromlist=["_attach_event_file_links"])._attach_event_file_links(
        [event],
        messages=[message],
        content_resolver=resolver,
    )

    assert attached[0].file_links == [
        EventFileLink(
            url="https://foo.feishu.cn/wiki/MgYnwgMIkiUDGGkjHYLcEMaEnhd",
            title="仓库摄像头录制 PRD",
            link_type="feishu_doc",
        )
    ]
    assert "《仓库摄像头录制 PRD》" in attached[0].title
    assert "《仓库摄像头录制 PRD》" in attached[0].content
    assert "MgYnwgMIkiUDGGkjHYLcEMaEnhd" not in attached[0].content


def test_runner_attaches_plain_attachment_by_reference_or_exact_name(tmp_path: Path) -> None:
    resolver = FeishuMessageContentResolver(config=RuntimeConfig(data_root=tmp_path / "data"))
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_file",
        sender_open_id="ou_self",
        sender_name="Me",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="file",
        text='<file key="file_1" name="友好换电管理方案.docx"/>',
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[
            AttachmentMeta(
                attachment_id="file_1",
                file_name="友好换电管理方案.docx",
                mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                file_size=123,
            )
        ],
        is_system=False,
    )
    event = WorkEvent(
        date="2026-06-22",
        event_id="evt1",
        title="同步友好换电管理方案并要求群转发",
        content="孙维晟将友好换电管理方案.docx文件发送至群内，并明确要求转发。",
        source_message_ids=["om_file"],
        referenced_link_ids=[],
        referenced_attachment_ids=["file_1"],
        file_links=[],
        object_hint="友好换电管理方案",
        retention_reason="follow_up_assigned",
        retention_detail="发送文件并指派转发动作。",
    )

    attached = __import__("src.worktrace.runner", fromlist=["_attach_event_file_links"])._attach_event_file_links(
        [event],
        messages=[message],
        content_resolver=resolver,
    )

    assert attached[0].file_links == [
        EventFileLink(
            url="",
            title="友好换电管理方案.docx",
            link_type="attachment",
        )
    ]
    assert "《友好换电管理方案.docx》" in attached[0].title
    assert "《友好换电管理方案.docx》" in attached[0].content

    without_reference = WorkEvent(
        date=event.date,
        event_id="evt2",
        title=event.title,
        content=event.content,
        source_message_ids=event.source_message_ids,
        object_hint=event.object_hint,
        retention_reason=event.retention_reason,
        retention_detail=event.retention_detail,
    )
    inferred = __import__("src.worktrace.runner", fromlist=["_attach_event_file_links"])._attach_event_file_links(
        [without_reference],
        messages=[message],
        content_resolver=resolver,
    )

    assert inferred[0].file_links == attached[0].file_links
    assert inferred[0].referenced_attachment_ids == ["file_1"]

    generic_event = WorkEvent(
        date=event.date,
        event_id="evt3",
        title="文件审核",
        content="发送相关文件并要求审核。",
        source_message_ids=event.source_message_ids,
        object_hint="管理方案",
        retention_reason=event.retention_reason,
        retention_detail=event.retention_detail,
    )
    unattached = __import__("src.worktrace.runner", fromlist=["_attach_event_file_links"])._attach_event_file_links(
        [generic_event],
        messages=[message],
        content_resolver=resolver,
    )

    assert unattached[0].file_links == []


def test_selected_document_survives_summary_rewrite() -> None:
    message = LinkSource().fetch_conversation_messages("2026-06-22", ["oc_1"])[0]
    event = WorkEvent(
        date="2026-06-22", event_id="evt1", title="确认上线安排",
        content="已确认排期并落实后续跟进。", object_hint="上线排期",
        source_message_ids=["om_1"], referenced_link_ids=["om_1#link1"],
    )
    attached = _attach_event_file_links([event], messages=[message], content_resolver=LinkResolver())
    assert [(link.title, link.url) for link in attached[0].file_links] == [
        ("发布方案", "https://foo.feishu.cn/docx/abc")
    ]


def test_file_association_is_limited_to_event_evidence() -> None:
    message = LinkSource().fetch_conversation_messages("2026-06-22", ["oc_1"])[0]
    unrelated = replace(message, message_id="om_other", links=[
        LinkMeta("https://foo.feishu.cn/docx/unrelated", "无关方案", "feishu_doc")
    ], attachments=[AttachmentMeta("file_other", "无关附件.pdf", "application/pdf", 1)])
    event = WorkEvent(
        date="2026-06-22", event_id="evt1", title="确认上线",
        content="https://foo.feishu.cn/docx/unrelated 无关附件.pdf",
        source_message_ids=["om_1"],
        referenced_link_ids=["om_other#link1"], referenced_attachment_ids=["file_other"],
    )
    attached = _attach_event_file_links([event], messages=[message, unrelated], content_resolver=LinkResolver())
    assert attached[0].file_links == []
    assert attached[0].referenced_attachment_ids == []


@pytest.mark.parametrize("first_title", ["", "https://foo.feishu.cn/docx/abc"])
def test_multiple_documents_and_same_named_distinct_attachments_are_preserved(first_title: str) -> None:
    message = LinkSource().fetch_conversation_messages("2026-06-22", ["oc_1"])[0]
    message = replace(message, links=[
        LinkMeta("https://foo.feishu.cn/docx/abc", first_title, "feishu_doc"),
        LinkMeta("https://foo.feishu.cn/docx/def", "验收记录", "feishu_doc"),
        LinkMeta("https://foo.feishu.cn/docx/abc", "发布方案", "feishu_doc"),
    ], attachments=[
        AttachmentMeta("file_a", "验收.xlsx", "application/octet-stream", 1),
        AttachmentMeta("file_b", "验收.xlsx", "application/octet-stream", 2),
    ])
    event = WorkEvent(
        date="2026-06-22", event_id="evt1", title="完成验收", content="已核对并确认结果。",
        source_message_ids=["om_1"],
        referenced_link_ids=["om_1#link3", "om_1#link2", "om_1#link1", "om_1#link2"],
        referenced_attachment_ids=["file_a", "file_b", "file_a"],
    )
    attached = _attach_event_file_links([event], messages=[message], content_resolver=LinkResolver())[0]
    assert [(link.title, link.url) for link in attached.file_links] == [
        ("发布方案", "https://foo.feishu.cn/docx/abc"),
        ("验收记录", "https://foo.feishu.cn/docx/def"),
        ("验收.xlsx", ""), ("验收.xlsx", ""),
    ]
    assert attached.referenced_attachment_ids == ["file_a", "file_b"]


def test_runner_keeps_document_from_expanded_reply_and_requested_report_date(tmp_path: Path) -> None:
    class RelatedSource(LinkSource):
        def fetch_conversation_messages(self, target_date, conversation_ids):
            message = super().fetch_conversation_messages(target_date, conversation_ids)[0]
            return [replace(message, text="确认安排", links=[], reply_to_message_id="om_prior")]

        def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
            message = super().fetch_conversation_messages("2026-06-21", [conversation_id])[0]
            return [replace(message, message_id="om_prior", sender_open_id="ou_other",
                            send_time="2026-06-21T18:00:00+08:00")]

    class RelatedAnalyzer(LinkAnalyzer):
        def __init__(self):
            self.calls = 0

        def analyze_batch(self, target_date, batch_input):
            self.calls += 1
            if self.calls == 1:
                return BatchAnalysisResult(context_requests=[ContextRequest(
                    slice_id=batch_input.slices[0].slice_id, request_type="earlier_messages",
                    target_message_ids=["om_1"], target_attachment_ids=[], reason="补齐回复对象", limit=1,
                )])
            result = super().analyze_batch(target_date, batch_input)
            return replace(result, candidate_events=[replace(
                result.candidate_events[0], source_message_ids=["om_prior", "om_1"],
                referenced_link_ids=["om_prior#link1"],
                topic="确认上线安排", content="已确认排期并落实跟进。",
                object_hint="上线排期", retention_detail="形成明确安排。",
            )])

    config = RuntimeConfig(data_root=tmp_path / "data")
    runner = DailyTraceRunner(config=config, dependencies=RuntimeDependencies(
        chat_source=RelatedSource(), content_resolver=LinkResolver(), analyzer=RelatedAnalyzer(),
        delivery_channel=LinkDelivery(), event_store=MarkdownEventStore(config=config),
    ))
    result = runner.run("2026-06-22")
    assert result.output_path is not None
    markdown = Path(result.output_path).read_text(encoding="utf-8")
    assert "[发布方案](https://foo.feishu.cn/docx/abc)" in markdown
    assert result.target_date == "2026-06-22"
    assert runner.dependencies.event_store.read_day("2026-06-22").date == "2026-06-22"


@pytest.mark.parametrize("use_fallback", [False, True])
def test_runner_keeps_external_reply_document_in_segment_and_fallback_paths(
    tmp_path: Path, use_fallback: bool,
) -> None:
    class ExternalSource(LinkSource):
        def fetch_conversation_messages(self, target_date, conversation_ids):
            original = super().fetch_conversation_messages(target_date, conversation_ids)[0]
            return [replace(original, text="确认安排", links=[], conversation_id="p2p_1",
                            conversation_mode="p2p", reply_to_message_id="om_prior")]

        def fetch_messages_by_ids(self, conversation_id, message_ids):
            if "om_prior" not in message_ids:
                return []
            original = super().fetch_conversation_messages("2026-06-21", [conversation_id])[0]
            return [replace(original, message_id="om_prior", conversation_id="p2p_1",
                            sender_open_id="ou_other", send_time="2026-06-21T18:00:00+08:00")]

    class ExternalAnalyzer(LinkAnalyzer):
        def segment_conversation(self, **kwargs):
            if use_fallback:
                return ConversationSegmentationResult()
            return ConversationSegmentationResult(segments=[ConversationSegment(
                segment_id="turn", primary_message_ids=[message.message_id for message in kwargs["messages"]],
            )])

        def candidate(self):
            return SourceBackedEventDraft(
                draft_id="d1", date="2026-06-22", topic="确认上线安排", content="已确认上线排期。",
                source_message_ids=["om_prior", "om_1"], source_conversation_id="p2p_1", source_slice_id="",
                confidence=0.9, action_label="确认", object_hint="上线排期",
                retention_reason="decision_made", retention_detail="形成明确安排。",
                self_evidence_message_ids=["om_1"], referenced_link_ids=["om_prior#link1"],
                self_relations=[SelfRelationEvidence("initiated", ["om_1"])],
            )

        def analyze_segment_batch(self, batch):
            return BatchSegmentAnalysisResult(results=[BatchSegmentAnalysisItem(
                unit.segment_id, BatchAnalysisResult(candidate_events=[self.candidate()]),
            ) for unit in batch.segments])

        def analyze_anchor_batch(self, target_date, anchor_units):
            return BatchAnchorAnalysisResult(results=[BatchAnchorAnalysisItem(
                anchor_unit_id=unit.anchor_unit_id,
                analysis=AnchorAnalysisResult(anchor_status="completed", candidate_events=[self.candidate()]),
            ) for unit in anchor_units])

    config = RuntimeConfig(
        data_root=tmp_path / "data", anchor_retry_limit=0,
        self_relation_types=(EventMetadataItem("initiated", "发起", 10),),
    )
    runner = DailyTraceRunner(config=config, dependencies=RuntimeDependencies(
        chat_source=ExternalSource(), content_resolver=LinkResolver(), analyzer=ExternalAnalyzer(),
        delivery_channel=LinkDelivery(), event_store=MarkdownEventStore(config=config),
    ))
    result = runner.run("2026-06-22")
    assert result.event_count == 1, result.to_dict()
    markdown = Path(result.output_path).read_text(encoding="utf-8")
    assert "[发布方案](https://foo.feishu.cn/docx/abc)" in markdown
    assert runner.dependencies.event_store.read_day("2026-06-22").date == "2026-06-22"


def test_runner_merges_multiple_events_without_losing_member_documents(tmp_path: Path) -> None:
    class MultiSource(LinkSource):
        def fetch_conversation_messages(self, target_date, conversation_ids):
            first = super().fetch_conversation_messages(target_date, conversation_ids)[0]
            return [first, replace(first, message_id="om_2", send_time="2026-06-22T11:00:00+08:00",
                                   text="完成核对", links=[LinkMeta(
                                       "https://foo.feishu.cn/docx/def", "验收记录", "feishu_doc",
                                   )])]

    class MultiAnalyzer(LinkAnalyzer):
        def analyze_batch(self, target_date, batch_input):
            first = super().analyze_batch(target_date, batch_input).candidate_events[0]
            return BatchAnalysisResult(candidate_events=[first, replace(
                first, draft_id="draft-2", source_message_ids=["om_2"],
                topic="验收推进", content="完成验收核对", object_hint="验收结果",
                referenced_link_ids=["om_2#link1"], retention_detail="确认验收核对结果。",
            )])

        def merge_day_candidates(self, target_date, candidates, *, validation_feedback=""):
            return CrossConversationGroupResult(groups=[CrossConversationGroup(
                group_id="g1", draft_ids=[candidate.draft_id for candidate in candidates],
                primary_draft_id="draft-1", merge_reason="同一发布安排的确认和验收。",
                evidence_message_ids=["om_1", "om_2"],
            )])

        def request_function(self, prompt, *, function_spec, allow_oversized_input=False):
            result = super().request_function(prompt, function_spec=function_spec,
                                              allow_oversized_input=allow_oversized_input)
            if function_spec.request_kind == "personal_group_render":
                # Final wording deliberately omits every document name and URL.
                for group in result["groups"]:
                    for fact in group["fact_items"]:
                        fact["text"] = {
                            "topic": "确认整体安排", "content": "已确认安排并完成核对。",
                            "object_hint": "整体安排",
                        }.get(fact["field"], fact["text"])
            return result

    config = RuntimeConfig(data_root=tmp_path / "data")
    runner = DailyTraceRunner(config=config, dependencies=RuntimeDependencies(
        chat_source=MultiSource(), content_resolver=LinkResolver(), analyzer=MultiAnalyzer(),
        delivery_channel=LinkDelivery(), event_store=MarkdownEventStore(config=config),
    ))
    result = runner.run("2026-06-22")
    assert result.event_count == 1, result.to_dict()
    markdown = Path(result.output_path).read_text(encoding="utf-8")
    assert "已确认安排并完成核对。" in markdown
    assert markdown.count("[发布方案](https://foo.feishu.cn/docx/abc)") == 1
    assert markdown.count("[验收记录](https://foo.feishu.cn/docx/def)") == 1


@pytest.mark.parametrize("same_matter", [True, False])
def test_shared_document_in_expanded_context_triggers_cross_conversation_review(
    tmp_path: Path, same_matter: bool,
) -> None:
    class SharedDocumentSource(LinkSource):
        def list_target_conversations(self, target_date, self_identity):
            return [ConversationRef(conversation_id=value, conversation_name=value)
                    for value in ("oc_1", "oc_2")]

        def fetch_conversation_messages(self, target_date, conversation_ids):
            original = super().fetch_conversation_messages(target_date, conversation_ids)[0]
            return [
                replace(original, text="确认安排", links=[], reply_to_message_id="om_prior"),
                replace(original, message_id="om_2", conversation_id="oc_2",
                        conversation_name="验收群", send_time="2026-06-22T11:00:00+08:00",
                        text="核对完成", links=original.links + [LinkMeta(
                            "https://foo.feishu.cn/docx/def", "验收记录", "feishu_doc",
                        )]),
            ]

        def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
            original = super().fetch_conversation_messages("2026-06-21", [conversation_id])[0]
            return [replace(original, message_id="om_prior", sender_open_id="ou_other",
                            send_time="2026-06-21T18:00:00+08:00")]

    class SharedDocumentAnalyzer(LinkAnalyzer):
        def __init__(self):
            self.review_inputs = []

        def analyze_batch(self, target_date, batch_input):
            unit = batch_input.slices[0]
            if unit.conversation_id == "oc_1" and "om_prior" not in {
                message.message_id for message in unit.messages
            }:
                return BatchAnalysisResult(context_requests=[ContextRequest(
                    slice_id=unit.slice_id, request_type="earlier_messages",
                    target_message_ids=["om_1"], target_attachment_ids=[],
                    reason="补齐回复对象", limit=1,
                )])
            candidate = super().analyze_batch(target_date, batch_input).candidate_events[0]
            first = unit.conversation_id == "oc_1"
            return BatchAnalysisResult(candidate_events=[replace(
                candidate, draft_id="d1" if first else "d2",
                topic="确认排期" if first else "核对结果", content="已形成明确结论。",
                object_hint="执行安排", retention_detail="确认处理结果。",
                source_message_ids=["om_prior", "om_1"] if first else ["om_2"],
                source_conversation_id=unit.conversation_id,
                referenced_link_ids=["om_prior#link1"] if first else ["om_2#link1", "om_2#link2"],
            )])

        def merge_day_candidates(self, target_date, candidates, *, validation_feedback=""):
            # Initial grouping keeps the two conversations separate.
            assert {candidate.source_conversation_id for candidate in candidates} == {"oc_1", "oc_2"}
            return CrossConversationGroupResult(groups=[CrossConversationGroup(
                group_id=f"g{index}", draft_ids=[candidate.draft_id],
                primary_draft_id=candidate.draft_id, merge_reason="单条保留",
            ) for index, candidate in enumerate(candidates, start=1)])

        def request_function(self, prompt, *, function_spec, allow_oversized_input=False):
            result = super().request_function(prompt, function_spec=function_spec,
                                              allow_oversized_input=allow_oversized_input)
            if function_spec.request_kind == "day_group_review":
                payload = json.loads(prompt)
                self.review_inputs.append(payload)
                for resolution in result["relation_resolutions"]:
                    resolution["reason"] = "共用文件，但聊天内容涉及不同的处理事项。"
                if same_matter:
                    candidates = payload["candidates"]
                    draft_ids = [item["draft_id"] for item in candidates]
                    result["merged_groups"] = [{
                        "draft_ids": draft_ids, "primary_draft_id": draft_ids[0],
                        "common_object": "执行安排", "semantic_reasons": ["continuous_action"],
                        "reason_detail": "两组分别确认排期和核对执行结果，属于同一过程。",
                        "member_connections": [{
                            "draft_id": item["draft_id"], "connection_detail": "参与同一执行过程。",
                            "evidence_message_ids": item["source_message_ids"],
                        } for item in candidates],
                    }]
                    result["singleton_draft_ids"] = []
                    for resolution in result["relation_resolutions"]:
                        resolution.update(decision="merged", connected_draft_ids=draft_ids,
                                          reason="聊天内容证明属于同一执行过程。")
            if function_spec.request_kind == "personal_group_render":
                for group in result["groups"]:
                    for fact in group["fact_items"]:
                        fact["text"] = {
                            "topic": "确认整体安排", "content": "已确认安排并完成核对。",
                            "object_hint": "整体安排",
                        }.get(fact["field"], fact["text"])
            return result

    config = RuntimeConfig(data_root=tmp_path / "data")
    analyzer = SharedDocumentAnalyzer()
    runner = DailyTraceRunner(config=config, dependencies=RuntimeDependencies(
        chat_source=SharedDocumentSource(), content_resolver=LinkResolver(), analyzer=analyzer,
        delivery_channel=LinkDelivery(), event_store=MarkdownEventStore(config=config),
    ))
    result = runner.run("2026-06-22")
    assert result.event_count == (1 if same_matter else 2), result.to_dict()
    assert len(analyzer.review_inputs) == 1
    review = analyzer.review_inputs[0]
    assert {item["draft_id"] for item in review["candidates"]} == {"d1", "d2"}
    assert any("om_prior" in item["source_message_ids"] for item in review["candidates"])
    assert any("shared_file" in item["relation_types"] for item in review["strong_relations"])
    markdown = Path(result.output_path).read_text(encoding="utf-8")
    assert markdown.count("[发布方案](https://foo.feishu.cn/docx/abc)") == (1 if same_matter else 2)
    assert markdown.count("[验收记录](https://foo.feishu.cn/docx/def)") == 1
    assert runner.dependencies.event_store.read_day("2026-06-22").date == "2026-06-22"
