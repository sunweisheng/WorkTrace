from __future__ import annotations

from pathlib import Path

from src.worktrace.config import RuntimeConfig
from src.worktrace.constants import DailyRunStatus
from src.worktrace.factories import RuntimeDependencies
from src.worktrace.models import SelfIdentity
from src.worktrace.runner import DailyTraceRunner
from src.worktrace.stores.markdown import MarkdownEventStore


class EmptySource:
    def get_self_identity(self):
        return SelfIdentity(open_id="ou_self", display_name="Me", source="fake")

    def list_target_conversations(self, target_date, self_identity):
        return []

    def fetch_conversation_messages(self, target_date, conversation_ids):
        return []

    def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
        return []


class EmptyResolver:
    def to_text(self, message):
        return message.text

    def load_attachment_text_if_needed(self, message, attachment_ids, hint):
        return None


class EmptyAnalyzer:
    def analyze_batch(self, target_date, batch_input):
        raise AssertionError("Should not analyze empty day")

    def merge_day_candidates(self, target_date, candidates):
        raise AssertionError("Should not merge empty day")


def test_runner_empty_day_is_success(tmp_path: Path) -> None:
    config = RuntimeConfig(data_root=tmp_path / "data")
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=EmptySource(),
            content_resolver=EmptyResolver(),
            analyzer=EmptyAnalyzer(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert result.event_count == 0
