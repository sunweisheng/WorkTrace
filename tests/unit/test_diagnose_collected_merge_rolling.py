from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import scripts.diagnose_collected_merge_rolling as diagnostic
from src.worktrace.config import RuntimeConfig
from src.worktrace.models import CollectedMergeResult, CollectedMergeRunResult


def _build_runner(
    tmp_path: Path,
    invoke,
) -> diagnostic.TracingCollectedMergeRunner:
    runner = diagnostic.TracingCollectedMergeRunner.__new__(
        diagnostic.TracingCollectedMergeRunner
    )
    runner.config = RuntimeConfig()
    runner.trace_dir = tmp_path
    runner.step_summaries = []
    runner._invoke_collected_merge_with_retry = invoke
    runner._fill_collected_merge_group_metadata = (
        lambda source_events, result: (result, [])
    )
    runner._materialize_events = lambda target_date, source_events, result: []
    runner._filter_sensitive_events = lambda target_date, events: (events, [])
    return runner


def test_diagnostic_step_is_running_during_llm_call_and_success_afterward(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    step_path = tmp_path / "step-001.json"

    def invoke(target_date, source_events, deterministic_groups, **kwargs):
        running = json.loads(step_path.read_text(encoding="utf-8"))
        assert running["status"] == "running"
        assert running["completed_at_utc"] is None
        return CollectedMergeResult(), []

    runner = _build_runner(tmp_path, invoke)
    monkeypatch.setattr(
        diagnostic,
        "build_collected_render_prompt",
        lambda *args, **kwargs: "diagnostic prompt",
    )
    monkeypatch.setattr(
        diagnostic,
        "repair_collected_merge_result",
        lambda result, source_events, deterministic_groups: (result, []),
    )
    monkeypatch.setattr(
        diagnostic,
        "filter_retained_work_events",
        lambda events: (events, []),
    )

    events, warnings = runner._merge_collected_event_batch(
        "2026-07-15",
        [],
        deterministic_groups=[],
    )

    captured = capsys.readouterr()
    completed = json.loads(step_path.read_text(encoding="utf-8"))
    assert events == []
    assert warnings == []
    assert completed["status"] == "success"
    assert completed["completed_at_utc"]
    assert completed["elapsed_ms"] >= 0
    assert 'status="running"' in captured.err
    assert 'status="success"' in captured.err


def test_diagnostic_step_records_failed_llm_call(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    def invoke(target_date, source_events, deterministic_groups, **kwargs):
        raise RuntimeError("simulated failure")

    runner = _build_runner(tmp_path, invoke)
    monkeypatch.setattr(
        diagnostic,
        "build_collected_render_prompt",
        lambda *args, **kwargs: "diagnostic prompt",
    )

    with pytest.raises(RuntimeError, match="simulated failure"):
        runner._merge_collected_event_batch(
            "2026-07-15",
            [],
            deterministic_groups=[],
        )

    captured = capsys.readouterr()
    failed = json.loads(
        (tmp_path / "step-001.json").read_text(encoding="utf-8")
    )
    assert failed["status"] == "failed"
    assert failed["error"] == {
        "type": "RuntimeError",
        "summary": "simulated failure",
    }
    assert 'status="failed"' in captured.err


def test_diagnostic_main_stops_before_read_and_model_after_preflight_failure(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    input_dir = tmp_path / "merge_inbox" / "2026" / "07" / "15"
    preflight_failure = CollectedMergeRunResult(
        status="failed",
        target_date="2026-07-15",
        input_dir=str(input_dir),
        output_path=None,
        source_file_count=1,
        source_event_count=1,
        merged_event_count=0,
        skipped_file_count=0,
        partial_file_count=0,
        warning_messages=[
            "Missing conversation evidence or manual edit marker: source.md (1 events)."
        ],
    )

    class FakeRunner:
        def build_input_dir(self, target_date):
            assert target_date == "2026-07-15"
            return input_dir

        def _preflight_conversation_evidence(self, *args, **kwargs):
            return preflight_failure

        def _read_source_events(self, *args, **kwargs):
            raise AssertionError("source reading must not continue after preflight failure")

    monkeypatch.setattr(
        diagnostic,
        "TracingCollectedMergeRunner",
        lambda **kwargs: FakeRunner(),
    )
    monkeypatch.setattr(
        diagnostic,
        "load_runtime_config_overrides",
        lambda config, cwd: RuntimeConfig(),
    )
    monkeypatch.setattr(
        diagnostic,
        "load_conversation_blacklist_overrides",
        lambda config, cwd: config,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "diagnose_collected_merge_rolling.py",
            "--date",
            "2026-07-15",
            "--owner",
            "管理者",
            "--output-dir",
            str(tmp_path / "trace"),
        ],
    )

    diagnostic.main()

    capsys.readouterr()
    summary = json.loads(
        (tmp_path / "trace" / "2026-07-15" / "summary.json").read_text(
            encoding="utf-8"
        )
    )
    summary_markdown = (
        tmp_path / "trace" / "2026-07-15" / "summary.md"
    ).read_text(encoding="utf-8")
    assert summary["status"] == "failed"
    assert summary["source_event_count_before_preflight"] == 1
    assert summary["source_event_count_after_source_filter"] == 0
    assert summary["steps"] == []
    assert "## Preflight Warnings" in summary_markdown
    assert "Missing conversation evidence or manual edit marker" in summary_markdown
