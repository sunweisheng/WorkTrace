from __future__ import annotations

from pathlib import Path

from src.worktrace.config import RuntimeConfig
from src.worktrace.constants import DailyRunStatus
from src.worktrace.errors import AnalyzerProtocolError
from src.worktrace.factories import RuntimeDependencies
from src.worktrace.models import ConversationRef, NormalizedMessage, SelfIdentity
from src.worktrace.runner import DailyTraceRunner
from src.worktrace.stores.markdown import MarkdownEventStore


class FailingSource:
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
                text="失败案例",
                reply_to_message_id=None,
                quote_message_id=None,
                links=[],
                attachments=[],
                is_system=False,
            )
        ]

    def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
        return []


class SimpleResolver:
    def to_text(self, message):
        return message.text

    def load_attachment_text_if_needed(self, message, attachment_ids, hint):
        return None


class FailingAnalyzer:
    def build_batch_prompt(self, batch_input):
        return "bad prompt"

    def analyze_batch(self, target_date, batch_input):
        raise AnalyzerProtocolError("bad json")

    def merge_day_candidates(self, target_date, candidates):
        raise AssertionError("Should not merge")


def test_runner_failure_modes(tmp_path: Path) -> None:
    config = RuntimeConfig(data_root=tmp_path / "data")
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=FailingSource(),
            content_resolver=SimpleResolver(),
            analyzer=FailingAnalyzer(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.FAILED.value
    assert result.output_path is None


def test_runner_dumps_failed_first_pass_debug_artifacts(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        conversation_debug_root=tmp_path / "debug",
    )
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=FailingSource(),
            content_resolver=SimpleResolver(),
            analyzer=FailingAnalyzer(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.FAILED.value
    pass_dir = tmp_path / "debug" / "2026-06-22" / "oc_1__om_1" / "pass_01"
    assert (pass_dir / "input.json").exists()
    assert (pass_dir / "prompt.txt").read_text(encoding="utf-8") == "bad prompt"
    meta = (pass_dir / "meta.json").read_text(encoding="utf-8")
    assert '"status": "failed"' in meta
    assert '"error_summary": "bad json"' in meta
