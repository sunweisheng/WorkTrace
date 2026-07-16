from __future__ import annotations

import json
from pathlib import Path

from scripts.replay_day_with_trace import _collect_review_artifact_summary, _parse_args
from scripts.report_replay_call_inputs import _review_records


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
