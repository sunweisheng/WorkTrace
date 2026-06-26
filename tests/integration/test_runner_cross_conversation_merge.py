from __future__ import annotations

from pathlib import Path

from src.worktrace.config import RuntimeConfig
from src.worktrace.constants import DailyRunStatus
from src.worktrace.factories import RuntimeDependencies
from src.worktrace.models import (
    BatchAnalysisResult,
    CrossConversationGroup,
    CrossConversationGroupResult,
    ConversationRef,
    NormalizedMessage,
    SelfIdentity,
    SourceBackedEventDraft,
)
from src.worktrace.runner import DailyTraceRunner
from src.worktrace.stores.markdown import MarkdownEventStore


class MergeSource:
    def get_self_identity(self):
        return SelfIdentity(open_id="ou_self", display_name="Me", source="fake")

    def list_target_conversations(self, target_date, self_identity):
        return [
            ConversationRef(conversation_id="oc_1", conversation_name="项目群1"),
            ConversationRef(conversation_id="oc_2", conversation_name="项目群2"),
            ConversationRef(conversation_id="oc_3", conversation_name="项目群3"),
        ]

    def fetch_conversation_messages(self, target_date, conversation_ids):
        return [
            NormalizedMessage(
                conversation_id="oc_1",
                conversation_name="项目群1",
                message_id="om_1",
                sender_open_id="ou_self",
                sender_name="Me",
                send_time="2026-06-22T10:00:00+08:00",
                message_type="text",
                text="release-123 排期确认",
                reply_to_message_id=None,
                quote_message_id=None,
                links=[],
                attachments=[],
                is_system=False,
            ),
            NormalizedMessage(
                conversation_id="oc_2",
                conversation_name="项目群2",
                message_id="om_2",
                sender_open_id="ou_self",
                sender_name="Me",
                send_time="2026-06-22T11:00:00+08:00",
                message_type="text",
                text="release-123 继续沟通",
                reply_to_message_id=None,
                quote_message_id=None,
                links=[],
                attachments=[],
                is_system=False,
            ),
            NormalizedMessage(
                conversation_id="oc_3",
                conversation_name="项目群3",
                message_id="om_3",
                sender_open_id="ou_self",
                sender_name="Me",
                send_time="2026-06-22T12:00:00+08:00",
                message_type="text",
                text="contract-888 合同沟通",
                reply_to_message_id=None,
                quote_message_id=None,
                links=[],
                attachments=[],
                is_system=False,
            ),
        ]

    def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
        return []


class MergeResolver:
    def to_text(self, message):
        return message.text

    def load_attachment_text_if_needed(self, message, attachment_ids, hint):
        return None


class MergeAnalyzer:
    def __init__(self):
        self.group_calls: list[list[str]] = []

    def analyze_batch(self, target_date, batch_input):
        message_id = batch_input.slices[0].messages[0].message_id
        if message_id == "om_1":
            return BatchAnalysisResult(
                candidate_events=[
                    SourceBackedEventDraft(
                        draft_id="d1",
                        date="2026-06-22",
                        topic="发布排期确认",
                        content="同步 release-123",
                        result="",
                        source_message_ids=["om_1"],
                        source_conversation_id="oc_1",
                        source_slice_id=batch_input.slices[0].slice_id,
                        confidence=0.9,
                    )
                ],
                context_requests=[],
            )
        if message_id == "om_2":
            return BatchAnalysisResult(
                candidate_events=[
                    SourceBackedEventDraft(
                        draft_id="d2",
                        date="2026-06-22",
                        topic="发布排期确认",
                        content="继续确认 release-123",
                        result="已确认",
                        source_message_ids=["om_2"],
                        source_conversation_id="oc_2",
                        source_slice_id=batch_input.slices[0].slice_id,
                        confidence=0.9,
                    )
                ],
                context_requests=[],
            )
        return BatchAnalysisResult(
            candidate_events=[
                SourceBackedEventDraft(
                    draft_id="d3",
                    date="2026-06-22",
                    topic="合同沟通",
                    content="跟进 contract-888",
                    result="",
                    source_message_ids=["om_3"],
                    source_conversation_id="oc_3",
                    source_slice_id=batch_input.slices[0].slice_id,
                    confidence=0.9,
                )
            ],
            context_requests=[],
        )

    def merge_day_candidates(self, target_date, candidates):
        self.group_calls.append([item.draft_id for item in candidates])
        return CrossConversationGroupResult(
            groups=[
                CrossConversationGroup(group_id="g1", draft_ids=["d1", "d2"]),
                CrossConversationGroup(group_id="g2", draft_ids=["d3"]),
            ]
        )

    def build_batch_prompt(self, batch_input):
        return "prompt"


def test_runner_groups_candidates_across_conversations_once(tmp_path: Path) -> None:
    analyzer = MergeAnalyzer()
    config = RuntimeConfig(data_root=tmp_path / "data")
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=MergeSource(),
            content_resolver=MergeResolver(),
            analyzer=analyzer,
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert result.event_count == 2
    assert analyzer.group_calls == [["d1", "d2", "d3"]]


class MissingDraftMergeAnalyzer(MergeAnalyzer):
    def merge_day_candidates(self, target_date, candidates):
        self.group_calls.append([item.draft_id for item in candidates])
        return CrossConversationGroupResult(
            groups=[
                CrossConversationGroup(group_id="g1", draft_ids=["d1", "d2"]),
            ]
        )


def test_runner_fails_when_merge_result_drops_candidate_draft(tmp_path: Path) -> None:
    analyzer = MissingDraftMergeAnalyzer()
    config = RuntimeConfig(data_root=tmp_path / "data")
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=MergeSource(),
            content_resolver=MergeResolver(),
            analyzer=analyzer,
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.FAILED.value
    assert "missing=['d3']" in result.error_summary
