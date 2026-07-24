from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.replay_day_with_trace as replay_debug
from scripts.hook_trace_wrapper import main as hook_trace_main
from scripts.replay_day_with_trace import (
    _collect_day_grouping_artifact_summary,
    _collect_llm_usage_summary,
    _collect_review_artifact_summary,
    _parse_args,
    _run_with_live_stderr,
)
from scripts.report_replay_call_inputs import (
    _anchor_fallback_records,
    _day_group_review_records,
    _merge_records,
    _read_completed_call_counts,
    _review_records,
)
from scripts.report_event_grouping_comparison import _build_comparison
from scripts.report_replay_timings import (
    _build_day_grouping_comparison,
    _collect_day_grouping_timing,
    _collect_event_extraction_timing,
    _collect_online_llm_summary,
    _collect_personal_fact_review_timing,
    _collect_stage_totals,
    _load_llm_usage_summary,
)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def test_replay_subprocess_streams_stderr_and_keeps_captured_logs(
    tmp_path: Path,
    capsys,
) -> None:
    stdout_path = tmp_path / "stdout.json"
    stderr_path = tmp_path / "stderr.log"

    completed = _run_with_live_stderr(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "print('stage started', file=sys.stderr, flush=True); "
                "print('{\"status\":\"ok\"}')"
            ),
        ],
        cwd=tmp_path,
        env=dict(os.environ),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )

    captured = capsys.readouterr()
    assert completed.returncode == 0
    assert json.loads(completed.stdout) == {"status": "ok"}
    assert "stage started" in captured.err
    assert "stage started" in completed.stderr
    assert stderr_path.read_text(encoding="utf-8") == completed.stderr


def test_hook_trace_writes_running_state_before_call_and_success_afterward(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    trace_root = tmp_path / "trace"
    counter_path = trace_root / "counter.txt"
    meta_path = trace_root / "llm_calls" / "call_001" / "meta.json"
    child_code = (
        "import json, sys; "
        "from pathlib import Path; "
        f"meta=json.loads(Path({str(meta_path)!r}).read_text(encoding='utf-8')); "
        "assert meta['status'] == 'running'; "
        "print('model stage active', file=sys.stderr, flush=True); "
        "print(json.dumps({'status': 'ok'}))"
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("test prompt"))

    returncode = hook_trace_main(
        [
            "--trace-root",
            str(trace_root),
            "--counter-path",
            str(counter_path),
            "--target-date",
            "2026-07-15",
            "--hook-command",
            shlex.join([sys.executable, "-c", child_code]),
        ]
    )

    captured = capsys.readouterr()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert returncode == 0
    assert meta["status"] == "success"
    assert meta["returncode"] == 0
    assert meta["completed_at_utc"]
    assert "model stage active" in captured.err
    assert 'hook_llm.call status="running"' in captured.err
    assert 'hook_llm.call status="success"' in captured.err


@pytest.mark.parametrize(
    ("returncode", "expected_status"),
    [(0, "success"), (3, "failed")],
)
def test_replay_main_updates_run_status_from_subprocess_result(
    tmp_path: Path,
    capsys,
    monkeypatch,
    returncode: int,
    expected_status: str,
) -> None:
    trace_root = tmp_path / "trace"

    def fake_run(
        command,
        *,
        cwd,
        env,
        stdout_path,
        stderr_path,
    ):
        running = json.loads(
            (trace_root / "run_status.json").read_text(encoding="utf-8")
        )
        assert running["status"] == "running"
        stdout_text = json.dumps({"status": "success"})
        stdout_path.write_text(stdout_text, encoding="utf-8")
        with stderr_path.open("a", encoding="utf-8") as stream:
            stream.write("runner.stage.completed duration_ms=1 stage=\"test\"\n")
        return subprocess.CompletedProcess(
            command,
            returncode,
            stdout=stdout_text,
            stderr=stderr_path.read_text(encoding="utf-8"),
        )

    monkeypatch.setattr(replay_debug, "_run_with_live_stderr", fake_run)

    actual_returncode = replay_debug.main(
        [
            "--date",
            "2026-07-15",
            "--trace-root",
            str(trace_root),
            "--data-root",
            str(tmp_path / "data"),
        ]
    )

    capsys.readouterr()
    final_status = json.loads(
        (trace_root / "run_status.json").read_text(encoding="utf-8")
    )
    assert actual_returncode == returncode
    assert final_status["status"] == expected_status
    assert final_status["returncode"] == returncode
    assert final_status["completed_at_utc"]


def test_replay_summary_collects_review_artifact_status(tmp_path: Path) -> None:
    debug_root = tmp_path / "conversation_debug"
    artifact_path = debug_root / "2026-07-15" / "personal_fact_review.json"
    _write_json(
        artifact_path,
        {
            "summary": {
                "selected_candidate_count": 1,
                "revised_candidate_count": 1,
            },
            "batches": [
                {"status": "failed"},
                {"status": "success"},
            ],
            "error_summary": "",
        },
    )

    summary = _collect_review_artifact_summary(debug_root, "2026-07-15")

    fact_review = summary["personal_fact_review"]
    assert fact_review["exists"] is True
    assert fact_review["attempt_count"] == 2
    assert fact_review["failed_attempt_count"] == 1
    assert fact_review["summary"]["revised_candidate_count"] == 1
    assert summary["retention_review"]["exists"] is False


def test_replay_args_support_isolated_analyzer_runs(tmp_path: Path) -> None:
    args = _parse_args(
        [
            "--date",
            "2026-07-15",
            "--analyzer-backend",
            "codex",
            "--codex-stdin-mode",
            "--data-root",
            str(tmp_path / "codex-data"),
        ]
    )

    assert args.analyzer_backend == "codex"
    assert args.codex_stdin_mode is True
    assert args.data_root == str(tmp_path / "codex-data")


def test_replay_summary_collects_llm_usage_by_request_kind(tmp_path: Path) -> None:
    debug_root = tmp_path / "conversation_debug"
    _write_json(
        debug_root / "2026-07-15" / "llm_usage.json",
        {
            "status": "success",
            "usage": {
                "request_count": 3,
                "total_tokens": 90,
                "by_request_kind": {
                    "personal_fact_review": {
                        "request_count": 2,
                        "total_tokens": 70,
                    },
                    "image_summary": {
                        "request_count": 1,
                        "total_tokens": 20,
                    },
                },
            },
            "requests": [
                {
                    "request_kind": "personal_fact_review",
                    "duration_ms": 4000.0,
                    "total_tokens": 30,
                },
                {
                    "request_kind": "image_summary",
                    "duration_ms": 1000.0,
                    "total_tokens": 20,
                },
                {
                    "request_kind": "personal_fact_review",
                    "duration_ms": 5000.0,
                    "total_tokens": 40,
                },
            ],
        },
    )

    summary = _collect_llm_usage_summary(debug_root, "2026-07-15")

    assert summary["request_count"] == 3
    assert summary["duration_ms"]["total"] == 10000.0
    fact_review = summary["by_request_kind"]["personal_fact_review"]
    assert fact_review["request_count"] == 2
    assert fact_review["duration_ms"]["max"] == 5000.0
    assert fact_review["token_usage"]["total_tokens"] == 70


def test_timing_report_uses_usage_types_and_separates_parallel_wall_clock() -> None:
    summary = {
        "llm_usage_summary": {
            "requests": [
                {
                    "request_kind": "personal_fact_review",
                    "duration_ms": 4000.0,
                    "prompt_chars": 100,
                    "total_tokens": 30,
                },
                {
                    "request_kind": "personal_fact_review",
                    "duration_ms": 5000.0,
                    "prompt_chars": 120,
                    "total_tokens": 40,
                },
            ],
            "by_request_kind": {
                "personal_fact_review": {
                    "token_usage": {"request_count": 2, "total_tokens": 70}
                }
            },
        },
        "timing_summary": {
            "events": [
                {
                    "event": "runner.stage.completed",
                    "duration_ms": 4000.0,
                    "raw_line": 'runner.stage.completed duration_ms=4000 stage="personal_fact_review"',
                },
                {
                    "event": "runner.stage.completed",
                    "duration_ms": 5000.0,
                    "raw_line": 'runner.stage.completed duration_ms=5000 stage="personal_fact_review"',
                },
                {
                    "event": "runner.stage.completed",
                    "duration_ms": 5100.0,
                    "raw_line": 'runner.stage.completed duration_ms=5100 stage="personal_fact_review_all"',
                },
            ]
        },
    }

    online = _collect_online_llm_summary(summary)
    fact_timing = _collect_personal_fact_review_timing(summary)

    assert online["source"] == "llm_usage.json"
    assert online["by_request_kind"]["personal_fact_review"]["token_usage"][
        "total_tokens"
    ] == 70
    assert fact_timing["batch_accumulated_ms"]["total"] == 9000.0
    assert fact_timing["wall_clock_ms"]["total"] == 5100.0
    assert fact_timing["accumulated_to_wall_clock_ratio"] == 1.765


def test_timing_report_prefers_event_extraction_wall_clock_over_parallel_sum() -> None:
    summary = {
        "timing_summary": {
            "totals_by_event_ms": {"runner.run.completed": 583743.0},
            "events": [
                {
                    "event": "runner.stage.completed",
                    "duration_ms": 700000.0,
                    "raw_line": 'runner.stage.completed duration_ms=700000 stage="analyze_segment_batch"',
                },
                {
                    "event": "runner.stage.completed",
                    "duration_ms": 678350.7,
                    "raw_line": 'runner.stage.completed duration_ms=678350.7 stage="analyze_segment_batch"',
                },
                {
                    "event": "runner.stage.completed",
                    "duration_ms": 275878.0,
                    "raw_line": 'runner.stage.completed duration_ms=275878 stage="analyze_segment_batches_all"',
                },
            ],
        }
    }

    stages = _collect_stage_totals(summary)
    timing = _collect_event_extraction_timing(summary)
    stage_by_name = {item["stage"]: item for item in stages}

    wall_clock = stage_by_name["analyze_segment_batches_all"]
    accumulated = stage_by_name["analyze_segment_batch"]
    assert stages.index(wall_clock) < stages.index(accumulated)
    assert wall_clock["timing_basis"] == "wall_clock"
    assert wall_clock["share_of_runner_total_pct"] == 47.26
    assert accumulated["timing_basis"] == "parallel_batch_accumulated"
    assert accumulated["share_of_runner_total_pct"] is None
    assert accumulated["elapsed_stage"] == "analyze_segment_batches_all"
    assert timing == {
        "batch_accumulated_ms": 1378350.7,
        "wall_clock_ms": 275878.0,
        "wall_clock_available": True,
        "accumulated_to_wall_clock_ratio": 4.996,
    }


def test_timing_report_marks_missing_event_extraction_wall_clock_as_unavailable() -> None:
    summary = {
        "timing_summary": {
            "events": [
                {
                    "event": "runner.stage.completed",
                    "duration_ms": 1378350.7,
                    "raw_line": 'runner.stage.completed duration_ms=1378350.7 stage="analyze_segment_batch"',
                }
            ]
        }
    }

    assert _collect_event_extraction_timing(summary) == {
        "batch_accumulated_ms": 1378350.7,
        "wall_clock_ms": None,
        "wall_clock_available": False,
        "accumulated_to_wall_clock_ratio": None,
    }


def test_timing_report_loads_usage_for_older_replay_summary(tmp_path: Path) -> None:
    _write_json(
        tmp_path
        / "conversation_debug"
        / "2026-07-15"
        / "llm_usage.json",
        {
            "usage": {
                "by_request_kind": {
                    "personal_fact_review": {
                        "request_count": 1,
                        "total_tokens": 42,
                    }
                }
            },
            "requests": [
                {
                    "request_kind": "personal_fact_review",
                    "duration_ms": 4200.0,
                    "total_tokens": 42,
                }
            ],
        },
    )

    usage = _load_llm_usage_summary(tmp_path, "2026-07-15")

    assert usage["requests"][0]["request_kind"] == "personal_fact_review"
    assert usage["by_request_kind"]["personal_fact_review"]["token_usage"][
        "total_tokens"
    ] == 42


def test_call_input_counts_exclude_image_summaries_from_text(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "summary.json",
        {
            "timing_summary": {
                "events": [
                    {
                        "event": "online_llm.request.completed",
                        "raw_line": 'request_kind="personal_fact_review" prompt_chars=100',
                    },
                    {
                        "event": "online_llm.request.completed",
                        "raw_line": 'request_kind="image_summary" image_bytes=200',
                    },
                ]
            }
        },
    )

    assert _read_completed_call_counts(tmp_path) == {
        "total": 2,
        "text": 1,
        "image": 1,
    }


def test_call_input_report_includes_anchor_fallback_attempts(tmp_path: Path) -> None:
    debug_root = tmp_path / "conversation_debug" / "2026-07-15"
    input_path = (
        debug_root
        / "_anchor_fallback"
        / "conversation"
        / "window"
        / "attempt-01"
        / "input.json"
    )
    _write_json(
        input_path,
        {
            "anchor_units": [
                {
                    "messages": [
                        {
                            "message_id": "m1",
                            "send_time": "2026-07-15T09:00:00+08:00",
                            "sender_name": "测试用户",
                            "text": "检查回放输入",
                        }
                    ]
                }
            ]
        },
    )
    _write_json(input_path.parent / "failure.json", {"error": "invalid output"})

    records = _anchor_fallback_records(
        debug_root,
        max_excerpts=6,
        max_chars=120,
    )

    assert len(records) == 1
    assert records[0].category == "分段失败直接提炼（第 01 次，failed）"
    assert records[0].item_count == 1


def test_call_input_report_includes_each_review_attempt(tmp_path: Path) -> None:
    debug_root = tmp_path / "conversation_debug" / "2026-07-15"
    _write_json(
        debug_root / "personal_fact_review.json",
        {
            "summary": {},
            "batches": [
                {
                    "batch_id": "personal-fact-review-001",
                    "attempt": 0,
                    "status": "failed",
                    "candidates": [
                        {
                            "draft_id": "d1",
                            "before": {
                                "topic": "设备流程审核与修改",
                                "content": "包含需要复核的对象和动作。",
                            },
                        }
                    ],
                },
                {
                    "batch_id": "personal-fact-review-001",
                    "attempt": 1,
                    "status": "success",
                    "candidates": [
                        {
                            "draft_id": "d1",
                            "before": {
                                "topic": "设备流程审核与修改",
                                "content": "包含需要复核的对象和动作。",
                            },
                        }
                    ],
                },
            ],
            "error_summary": "",
        },
    )
    _write_json(
        debug_root / "retention_review.json",
        {
            "summary": {},
            "batches": [
                {
                    "batch_id": "retention-review-001",
                    "attempt": 0,
                    "status": "success",
                    "candidates": [
                        {
                            "draft_id": "d2",
                            "before": {
                                "topic": "临时协作确认",
                                "content": "确认是否存在实质工作。",
                            },
                        }
                    ],
                }
            ],
            "error_summary": "",
        },
    )

    records = _review_records(
        debug_root,
        max_excerpts=6,
        max_chars=120,
    )

    assert len(records) == 3
    assert [item.category for item in records] == [
        "临时协作复核（第 1 次，success）",
        "个人事实复核（第 1 次，failed）",
        "个人事实复核（第 2 次，success）",
    ]
    assert records[1].item_count == 1
    assert "设备流程审核与修改" in records[1].content_summary


def test_replay_summary_reads_new_and_legacy_day_grouping_artifacts(
    tmp_path: Path,
) -> None:
    date_root = tmp_path / "conversation_debug" / "2026-07-15"
    merge_root = date_root / "_merge_day_candidates"
    _write_json(
        merge_root / "grouping_attempts.json",
        {
            "attempts": [
                {"status": "invalid"},
                {"status": "success"},
                {"status": "repaired"},
            ]
        },
    )
    _write_json(
        merge_root / "day_group_review.json",
        {"attempts": [{"status": "failed"}, {"status": "success"}]},
    )
    _write_json(
        merge_root / "resolved_groups.json",
        {
            "groups": [{"draft_ids": ["d1"]}, {"draft_ids": ["d2"]}],
            "warnings": ["review failed"],
            "summary": {"candidate_count": 2, "final_group_count": 2},
        },
    )

    current = _collect_day_grouping_artifact_summary(
        tmp_path / "conversation_debug",
        "2026-07-15",
    )

    assert current["legacy_trace"] is False
    assert current["grouping_attempt_count"] == 3
    assert current["grouping_failed_attempt_count"] == 1
    assert current["grouping_repair_count"] == 1
    assert current["review_attempt_count"] == 2
    assert current["review_failed_attempt_count"] == 1
    assert current["summary"]["candidate_count"] == 2

    (merge_root / "grouping_attempts.json").unlink()
    (merge_root / "day_group_review.json").unlink()
    _write_json(
        merge_root / "resolved_groups.json",
        {"groups": [{"draft_ids": ["d1"], "workstream_name": "旧字段"}]},
    )

    legacy = _collect_day_grouping_artifact_summary(
        tmp_path / "conversation_debug",
        "2026-07-15",
    )

    assert legacy["legacy_trace"] is True
    assert legacy["grouping_attempt_count"] == 0
    assert legacy["review_attempt_count"] == 0
    assert legacy["summary"] is None


def test_day_grouping_timing_separates_accumulated_requests_and_wall_clock() -> None:
    summary = {
        "llm_usage_summary": {
            "requests": [
                {"request_kind": "day_candidate_merge", "duration_ms": 14000.0},
                {"request_kind": "day_group_review", "duration_ms": 9000.0},
                {"request_kind": "day_group_review", "duration_ms": 8000.0},
                {"request_kind": "workstream_assignment", "duration_ms": 25000.0},
                {
                    "request_kind": "unassigned_workstream_assignment",
                    "duration_ms": 14000.0,
                },
            ]
        },
        "timing_summary": {
            "events": [
                {
                    "event": "runner.stage.completed",
                    "duration_ms": 9000.0,
                    "raw_line": 'runner.stage.completed duration_ms=9000 stage="day_group_review"',
                },
                {
                    "event": "runner.stage.completed",
                    "duration_ms": 8000.0,
                    "raw_line": 'runner.stage.completed duration_ms=8000 stage="day_group_review"',
                },
                {
                    "event": "runner.stage.completed",
                    "duration_ms": 9100.0,
                    "raw_line": 'runner.stage.completed duration_ms=9100 stage="day_group_review_all"',
                },
                {
                    "event": "runner.stage.completed",
                    "duration_ms": 23500.0,
                    "raw_line": 'runner.stage.completed duration_ms=23500 stage="merge_day_candidates"',
                },
            ]
        },
    }

    timing = _collect_day_grouping_timing(summary)

    assert timing["initial_grouping_request_accumulated_ms"]["total"] == 14000.0
    assert timing["local_review_request_accumulated_ms"]["total"] == 17000.0
    assert timing["local_review_wall_clock_ms"]["total"] == 9100.0
    assert timing["merge_day_candidates_wall_clock_ms"]["total"] == 23500.0
    assert timing["legacy_workstream_request_accumulated_ms"]["total"] == 39000.0


def test_day_grouping_timing_comparison_calculates_python_deltas() -> None:
    baseline = {
        "initial_grouping_request_accumulated_ms": {"total": 14000.0},
        "local_review_request_accumulated_ms": {"total": 0.0},
        "local_review_wall_clock_ms": {"total": 0.0},
        "merge_day_candidates_wall_clock_ms": {"total": 54000.0},
        "legacy_workstream_request_accumulated_ms": {"total": 39000.0},
    }
    current = {
        "initial_grouping_request_accumulated_ms": {"total": 15000.0},
        "local_review_request_accumulated_ms": {"total": 12000.0},
        "local_review_wall_clock_ms": {"total": 7000.0},
        "merge_day_candidates_wall_clock_ms": {"total": 23000.0},
        "legacy_workstream_request_accumulated_ms": {"total": 0.0},
    }

    comparison = _build_day_grouping_comparison(baseline, current)

    assert comparison["merge_day_candidates_wall_clock_ms"] == {
        "baseline_ms": 54000.0,
        "current_ms": 23000.0,
        "delta_ms": -31000.0,
        "delta_s": -31.0,
    }
    assert comparison["legacy_workstream_request_accumulated_ms"]["delta_ms"] == -39000.0


def test_call_input_report_reads_day_group_review_and_marks_legacy_calls(
    tmp_path: Path,
) -> None:
    debug_root = tmp_path / "conversation_debug" / "2026-07-15"
    merge_root = debug_root / "_merge_day_candidates"
    _write_json(
        merge_root / "input.json",
        {
            "candidates": [
                {"draft_id": "d1", "topic": "事项一", "content": "内容一"},
                {"draft_id": "d2", "topic": "事项二", "content": "内容二"},
            ]
        },
    )
    _write_json(
        merge_root / "day_group_review.json",
        {
            "attempts": [
                {
                    "component_id": "day-group-review-001",
                    "attempt": 1,
                    "backend": "online",
                    "status": "success",
                    "input": {
                        "candidate_draft_ids": ["d1", "d2"],
                        "groups": [
                            {"draft_ids": ["d1"]},
                            {"draft_ids": ["d2"]},
                        ],
                        "relation_reasons": [
                            {"relation_types": ["shared_message"]}
                        ],
                    },
                }
            ]
        },
    )
    _write_json(
        merge_root / "workstream_resolution_input.json",
        {"candidates": [{"draft_id": "d1", "topic": "旧事项"}]},
    )

    review_records = _day_group_review_records(
        debug_root,
        max_excerpts=6,
        max_chars=120,
    )
    merge_records = _merge_records(
        debug_root,
        max_excerpts=6,
        max_chars=120,
    )

    assert len(review_records) == 1
    assert review_records[0].item_count == 2
    assert "day-group-review-001" in review_records[0].category
    assert [item.category for item in merge_records] == [
        "全日初始分组",
        "旧版工作流归属",
    ]


def test_event_grouping_comparison_reports_coverage_merge_and_split_sets() -> None:
    baseline = {
        "candidate_ids": ["d1", "d2", "d3"],
        "candidate_count": 3,
        "group_count": 2,
        "singleton_group_count": 1,
        "multi_event_group_count": 1,
        "groups": [
            {"group_id": "old-1", "draft_ids": ["d1", "d2"]},
            {"group_id": "old-2", "draft_ids": ["d3"]},
        ],
        "coverage": {"valid": True},
        "review": {"component_count": 0, "request_count": 0, "components": []},
    }
    current = {
        "candidate_ids": ["d1", "d2", "d3"],
        "candidate_count": 3,
        "group_count": 2,
        "singleton_group_count": 1,
        "multi_event_group_count": 1,
        "groups": [
            {"group_id": "group-001", "draft_ids": ["d1"]},
            {
                "group_id": "group-002",
                "draft_ids": ["d2", "d3"],
                "merge_reason": "动作连续",
                "evidence_message_ids": ["m2", "m3"],
            },
        ],
        "coverage": {"valid": True},
        "review": {"component_count": 1, "request_count": 1, "components": []},
    }

    comparison = _build_comparison(
        baseline,
        current,
        target_date="2026-07-15",
    )

    assert comparison["changes"]["merged_candidate_pairs"] == [["d2", "d3"]]
    assert comparison["changes"]["split_candidate_pairs"] == [["d1", "d2"]]
    assert comparison["changes"]["merged_candidate_ids"] == ["d2", "d3"]
    assert comparison["changes"]["split_candidate_ids"] == ["d1", "d2"]
    assert len(comparison["changes"]["candidate_partition_changes"]) == 3
