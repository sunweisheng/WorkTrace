from __future__ import annotations

import json
from pathlib import Path

from scripts.replay_day_with_trace import (
    _collect_llm_usage_summary,
    _collect_review_artifact_summary,
    _parse_args,
)
from scripts.report_replay_call_inputs import (
    _anchor_fallback_records,
    _read_completed_call_counts,
    _review_records,
)
from scripts.report_replay_timings import (
    _collect_online_llm_summary,
    _collect_personal_fact_review_timing,
    _load_llm_usage_summary,
)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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
