from __future__ import annotations

from pathlib import Path

from src.worktrace.config import RuntimeConfig
from src.worktrace.constants import DailyRunStatus
from src.worktrace.factories import RuntimeDependencies
from src.worktrace.models import (
    BatchAnalysisResult,
    ContextRequest,
    ConversationRef,
    CrossConversationGroup,
    CrossConversationGroupResult,
    LinkMeta,
    LinkedFileTextBlock,
    MergedEventDraft,
    NormalizedMessage,
    SelfIdentity,
    SourceBackedEventDraft,
)
from src.worktrace.runner import DailyTraceRunner
from src.worktrace.stores.markdown import MarkdownEventStore
from tests.helpers import NullDelivery


class RetrySource:
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
                text="看附件",
                reply_to_message_id=None,
                quote_message_id=None,
                links=[],
                attachments=[],
                is_system=False,
            )
        ]

    def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
        return [
            NormalizedMessage(
                conversation_id="oc_1",
                conversation_name="项目群",
                message_id="om_2",
                sender_open_id="ou_other",
                sender_name="Alice",
                send_time="2026-06-22T10:01:00+08:00",
                message_type="text",
                text="补充上下文",
                reply_to_message_id="om_1",
                quote_message_id=None,
                links=[],
                attachments=[],
                is_system=False,
            )
        ]


class RetryResolver:
    def to_text(self, message):
        return message.text

    def extract_links(self, message):
        return list(message.links)

    def load_attachment_text_if_needed(self, message, attachment_ids, hint):
        return []

    def load_link_text_if_needed(self, message, link_ids, hint):
        return []


class RetryAnalyzer:
    def __init__(self):
        self.calls = 0

    def build_batch_prompt(self, batch_input):
        return "retry prompt"

    def analyze_batch(self, target_date, batch_input):
        self.calls += 1
        if self.calls == 1:
            return BatchAnalysisResult(
                candidate_events=[],
                context_requests=[
                    ContextRequest(
                        slice_id=batch_input.slices[0].slice_id,
                        request_type="later_messages",
                        target_message_ids=["om_1"],
                        target_attachment_ids=[],
                        reason="补上下文",
                        limit=1,
                    )
                ],
            )
        return BatchAnalysisResult(
            candidate_events=[
                SourceBackedEventDraft(
                    draft_id="d1",
                    date="2026-06-22",
                    topic="补充后确认",
                    content="补充上下文后完成分析",
                    source_message_ids=["om_1", "om_2"],
                    source_conversation_id="oc_1",
                    source_slice_id=batch_input.slices[0].slice_id,
                    confidence=0.9,
                    action_label="确认",
                    object_hint="附件上下文分析",
                    retention_reason="decision_made",
                    retention_detail="补充上下文后完成附件相关分析。",
                )
            ],
            context_requests=[],
        )

    def merge_day_candidates(self, target_date, candidates):
        return CrossConversationGroupResult(
            groups=[CrossConversationGroup(group_id="g1", draft_ids=["d1"])]
        )


def test_runner_retries_slice_until_context_is_resolved(tmp_path: Path) -> None:
    config = RuntimeConfig(data_root=tmp_path / "data")
    analyzer = RetryAnalyzer()
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=RetrySource(),
            content_resolver=RetryResolver(),
            analyzer=analyzer,
            delivery_channel=NullDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert result.event_count == 1
    assert result.skipped_slice_count == 0
    assert analyzer.calls == 2


def test_runner_retry_can_correct_reply_object_after_more_context(tmp_path: Path) -> None:
    class CorrectionSource(RetrySource):
        def fetch_conversation_messages(self, target_date, conversation_ids):
            return [
                NormalizedMessage(
                    conversation_id="oc_1",
                    conversation_name="项目群",
                    message_id="om_1",
                    sender_open_id="ou_other",
                    sender_name="Alice",
                    send_time="2026-06-22T10:00:00+08:00",
                    message_type="text",
                    text="上海大区报销单今天处理",
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
                    sender_open_id="ou_self",
                    sender_name="Me",
                    send_time="2026-06-22T10:01:00+08:00",
                    message_type="text",
                    text="这笔我先跟进",
                    reply_to_message_id="om_1",
                    quote_message_id=None,
                    links=[],
                    attachments=[],
                    is_system=False,
                ),
            ]

        def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
            return [
                NormalizedMessage(
                    conversation_id="oc_1",
                    conversation_name="项目群",
                    message_id="om_3",
                    sender_open_id="ou_other",
                    sender_name="Alice",
                    send_time="2026-06-22T10:02:00+08:00",
                    message_type="text",
                    text="不是上海，这是昆山的",
                    reply_to_message_id="om_2",
                    quote_message_id=None,
                    links=[],
                    attachments=[],
                    is_system=False,
                )
            ]

    class CorrectionAnalyzer(RetryAnalyzer):
        def analyze_batch(self, target_date, batch_input):
            self.calls += 1
            if self.calls == 1:
                return BatchAnalysisResult(
                    candidate_events=[],
                    context_requests=[
                        ContextRequest(
                            slice_id=batch_input.slices[0].slice_id,
                            request_type="later_messages",
                            target_message_ids=["om_2"],
                            reason="需要确认最终对象",
                            limit=1,
                        )
                    ],
                )
            return BatchAnalysisResult(
                candidate_events=[
                    SourceBackedEventDraft(
                        draft_id="d1",
                        date="2026-06-22",
                        topic="昆山报销单收款公司修改跟进",
                        content="补充后确认这笔是昆山报销单，并继续跟进收款公司修改。",
                        source_message_ids=["om_2", "om_3"],
                        source_conversation_id="oc_1",
                        source_slice_id=batch_input.slices[0].slice_id,
                        confidence=0.9,
                        action_label="跟进",
                        object_hint="昆山报销单收款公司修改",
                        retention_reason="follow_up_assigned",
                        retention_detail="补充后文明确说明这笔不是上海而是昆山。",
                    )
                ],
                context_requests=[],
            )

    config = RuntimeConfig(data_root=tmp_path / "data")
    analyzer = CorrectionAnalyzer()
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=CorrectionSource(),
            content_resolver=RetryResolver(),
            analyzer=analyzer,
            delivery_channel=NullDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")
    content = Path(result.output_path).read_text(encoding="utf-8")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert analyzer.calls == 2
    assert "昆山报销单收款公司修改跟进" in content
    assert "不是上海而是昆山" in content


def test_runner_retry_can_load_linked_file_text(tmp_path: Path) -> None:
    class LinkSource(RetrySource):
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
                            title="收款公司说明",
                            link_type="feishu_doc",
                        )
                    ],
                    attachments=[],
                    is_system=False,
                )
            ]

    class LinkResolver(RetryResolver):
        def load_link_text_if_needed(self, message, link_ids, hint):
            return [
                LinkedFileTextBlock(
                    link_id=link_ids[0],
                    message_id=message.message_id,
                    title="收款公司说明",
                    url="https://foo.feishu.cn/docx/abc",
                    text=f"文档明确写明收款公司需要修改。{hint}",
                )
            ]

    class LinkAnalyzer(RetryAnalyzer):
        def analyze_batch(self, target_date, batch_input):
            self.calls += 1
            if self.calls == 1:
                return BatchAnalysisResult(
                    candidate_events=[],
                    context_requests=[
                        ContextRequest(
                            slice_id=batch_input.slices[0].slice_id,
                            request_type="linked_file_text",
                            target_message_ids=["om_1"],
                            target_link_ids=["om_1#link1"],
                            reason="需要文档正文判断",
                            limit=1,
                        )
                    ],
                )
            return BatchAnalysisResult(
                candidate_events=[
                    SourceBackedEventDraft(
                        draft_id="d1",
                        date="2026-06-22",
                        topic="收款公司修改确认",
                        content="补读收款公司说明文档正文后，确认收款公司需要修改。",
                        source_message_ids=["om_1"],
                        source_conversation_id="oc_1",
                        source_slice_id=batch_input.slices[0].slice_id,
                        confidence=0.9,
                        action_label="确认",
                        object_hint="收款公司说明",
                        retention_reason="decision_made",
                        retention_detail="补读收款公司说明文档正文后完成确认。",
                        referenced_link_ids=["om_1#link1"],
                    )
                ],
                context_requests=[],
            )

    config = RuntimeConfig(data_root=tmp_path / "data")
    analyzer = LinkAnalyzer()
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=LinkSource(),
            content_resolver=LinkResolver(),
            analyzer=analyzer,
            delivery_channel=NullDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")
    content = Path(result.output_path).read_text(encoding="utf-8")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert analyzer.calls == 2
    assert "收款公司修改确认" in content
    assert "[收款公司说明](https://foo.feishu.cn/docx/abc)" in content
