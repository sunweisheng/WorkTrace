from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.worktrace.analyzers.codex import CodexAnalyzer
from src.worktrace.config import RuntimeConfig
from src.worktrace.errors import AnalyzerProtocolError
from src.worktrace.models import (
    AnalysisBatch,
    ConversationSlice,
    NormalizedMessage,
)


def sample_batch() -> AnalysisBatch:
    return AnalysisBatch(
        target_date="2026-06-23",
        batch_id="batch-001",
        retry_round=0,
        estimated_tokens=123,
        slices=[
            ConversationSlice(
                slice_id="slice-1",
                conversation_id="oc_1",
                conversation_name="项目群",
                anchor_message_ids=["om_1"],
                in_day_message_ids=["om_1"],
                messages=[
                    NormalizedMessage(
                        conversation_id="oc_1",
                        conversation_name="项目群",
                        message_id="om_1",
                        sender_open_id="ou_1",
                        sender_name="Alice",
                        send_time="2026-06-23T10:00:00+08:00",
                        message_type="text",
                        text="推进发布",
                        reply_to_message_id=None,
                        quote_message_id=None,
                    )
                ],
            )
        ],
    )


def test_codex_analyzer_surfaces_stderr_tail_on_failure(tmp_path: Path) -> None:
    def fake_runner(args, *, cwd=None, timeout=None, input_text=None):
        class Result:
            returncode = 1
            stdout = ""
            stderr = "line1\nline2\nline3\nline4\n"

        return Result()

    analyzer = CodexAnalyzer(
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            analyzer_backend="codex",
        ),
        command_runner=fake_runner,
        cwd=tmp_path,
    )

    with pytest.raises(AnalyzerProtocolError) as exc_info:
        analyzer.analyze_batch("2026-06-23", sample_batch())

    message = str(exc_info.value)
    assert "returncode=1" in message
    assert "line2 | line3 | line4" in message


def test_codex_analyzer_rejects_oversized_prompt_before_command(tmp_path: Path) -> None:
    calls = []

    def fake_runner(args, *, cwd=None, timeout=None, input_text=None):
        calls.append(args)
        raise AssertionError("command must not run")

    analyzer = CodexAnalyzer(
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            analyzer_backend="codex",
            max_model_input_tokens=1,
        ),
        command_runner=fake_runner,
        cwd=tmp_path,
    )

    with pytest.raises(AnalyzerProtocolError, match="max_model_input_tokens"):
        analyzer.analyze_batch("2026-06-23", sample_batch())

    assert calls == []
