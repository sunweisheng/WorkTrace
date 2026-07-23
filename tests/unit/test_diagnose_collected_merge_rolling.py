from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.diagnose_collected_merge_rolling as diagnostic
from src.worktrace.config import RuntimeConfig
from src.worktrace.models import CollectedMergeResult


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
