from __future__ import annotations

from pathlib import Path

from src.worktrace.config import RuntimeConfig
from src.worktrace.constants import DailyRunStatus
from src.worktrace.factories import RuntimeDependencies
from src.worktrace.models import (
    AttachmentMeta,
    BatchAnalysisResult,
    ConversationRef,
    EventFileLink,
    LinkMeta,
    NormalizedMessage,
    SelfIdentity,
    SourceBackedEventDraft,
    WorkEvent,
)
from src.worktrace.runner import DailyTraceRunner
from src.worktrace.resolvers.feishu_message import FeishuMessageContentResolver
from src.worktrace.stores.markdown import MarkdownEventStore


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


class LinkAnalyzer:
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

    def merge_day_candidates(self, target_date, candidates):
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


def test_runner_drops_selected_link_without_event_text_support(tmp_path: Path) -> None:
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

    assert attached[0].file_links == []


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


def test_runner_attaches_plain_attachment_file_name_when_event_mentions_it(tmp_path: Path) -> None:
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
