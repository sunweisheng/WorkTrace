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
    MergedEventDraft,
    NormalizedMessage,
    SelfIdentity,
    SourceBackedEventDraft,
)
from src.worktrace.runner import DailyTraceRunner
from src.worktrace.stores.markdown import MarkdownEventStore


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

    def load_attachment_text_if_needed(self, message, attachment_ids, hint):
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
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert result.event_count == 1
    assert result.skipped_slice_count == 0
    assert analyzer.calls == 2
