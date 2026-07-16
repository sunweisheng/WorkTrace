from __future__ import annotations

import json

from src.worktrace.cli import main
from src.worktrace.config import RuntimeConfig
from src.worktrace.constants import DailyRunStatus
from src.worktrace.models import (
    CollectedMergeOutput,
    CollectedMergeRunResult,
    DailyRunResult,
    RetentionReviewSummary,
)


def test_cli_returns_structured_json_for_invalid_input(capsys) -> None:
    exit_code = main(["--date", "2026/06/22"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 2
    assert payload["status"] == DailyRunStatus.INVALID_INPUT.value
    assert payload["output_path"] is None


def test_cli_returns_runner_result(capsys, tmp_path) -> None:
    def fake_preflight(config, *, cwd):
        from src.worktrace.preflight import PreflightReport

        return PreflightReport(ok=True, details={"cwd": str(cwd)})

    def fake_run(*, target_date, config):
        return DailyRunResult(
            target_date=target_date,
            conversation_count=2,
            message_count=8,
            slice_count=3,
            batch_count=1,
            event_count=2,
            skipped_slice_count=0,
            warning_count=0,
            status=DailyRunStatus.SUCCESS.value,
            output_path=str(tmp_path / "data/2026/06/2026-06-22.md"),
            error_summary="",
            self_delivery_status="pending",
            self_delivery_target="",
            self_delivery_error="",
            retention_review_summary=RetentionReviewSummary(
                selected_candidate_count=2,
                reviewed_candidate_count=2,
                kept_candidate_count=1,
                dropped_routine_count=1,
                review_batch_count=1,
            ),
        )

    exit_code = main(
        ["--date", "2026-06-22"],
        config=RuntimeConfig(data_root=tmp_path / "data"),
        preflight_func=fake_preflight,
        run_func=fake_run,
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["target_date"] == "2026-06-22"
    assert payload["status"] == DailyRunStatus.SUCCESS.value
    assert payload["event_count"] == 2
    assert payload["retention_review_summary"] == {
        "selected_candidate_count": 2,
        "reviewed_candidate_count": 2,
        "kept_candidate_count": 1,
        "dropped_routine_count": 1,
        "dropped_uncertain_count": 0,
        "review_batch_count": 1,
        "review_retry_count": 0,
    }
    assert payload["personal_fact_review_summary"] == {
        "selected_candidate_count": 0,
        "reviewed_candidate_count": 0,
        "confirmed_candidate_count": 0,
        "revised_candidate_count": 0,
        "dropped_unsupported_count": 0,
        "review_batch_count": 0,
        "review_retry_count": 0,
    }


def test_cli_supports_preflight_only_output(capsys, tmp_path) -> None:
    def fake_preflight(config, *, cwd):
        from src.worktrace.preflight import PreflightReport

        return PreflightReport(
            ok=True,
            details={
                "python": "ok",
                "reasoning_effort": "none",
            },
        )

    exit_code = main(
        ["--preflight"],
        config=RuntimeConfig(data_root=tmp_path / "data"),
        preflight_func=fake_preflight,
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["status"] == "ok"
    assert payload["error_summary"] == ""
    assert payload["details"]["reasoning_effort"] == "none"


def test_cli_debug_output_enables_default_debug_directory(capsys, tmp_path) -> None:
    captured_config = None

    def fake_preflight(config, *, cwd):
        from src.worktrace.preflight import PreflightReport

        nonlocal captured_config
        captured_config = config
        return PreflightReport(ok=True, details={"cwd": str(cwd)})

    def fake_run(*, target_date, config):
        nonlocal captured_config
        captured_config = config
        return DailyRunResult(
            target_date=target_date,
            conversation_count=0,
            message_count=0,
            slice_count=0,
            batch_count=0,
            event_count=0,
            skipped_slice_count=0,
            warning_count=0,
            status=DailyRunStatus.SUCCESS.value,
            output_path=str(tmp_path / "data/2026/06/2026-06-22.md"),
            error_summary="",
            self_delivery_status="success",
            self_delivery_target="ou_self",
            self_delivery_error="",
        )

    exit_code = main(
        ["--date", "2026-06-22", "--debug-output"],
        config=RuntimeConfig(data_root=tmp_path / "data"),
        preflight_func=fake_preflight,
        run_func=fake_run,
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["status"] == DailyRunStatus.SUCCESS.value
    assert captured_config is not None
    assert captured_config.conversation_debug_root == tmp_path / "data" / "debug" / "conversations"


def test_cli_debug_output_preserves_existing_debug_directory(capsys, tmp_path) -> None:
    existing_debug_root = tmp_path / "custom-debug"
    captured_config = None

    def fake_preflight(config, *, cwd):
        from src.worktrace.preflight import PreflightReport

        return PreflightReport(ok=True, details={"cwd": str(cwd)})

    def fake_run(*, target_date, config):
        nonlocal captured_config
        captured_config = config
        return DailyRunResult(
            target_date=target_date,
            conversation_count=0,
            message_count=0,
            slice_count=0,
            batch_count=0,
            event_count=0,
            skipped_slice_count=0,
            warning_count=0,
            status=DailyRunStatus.SUCCESS.value,
            output_path=str(tmp_path / "data/2026/06/2026-06-22.md"),
            error_summary="",
            self_delivery_status="success",
            self_delivery_target="ou_self",
            self_delivery_error="",
        )

    exit_code = main(
        ["--date", "2026-06-22", "--debug-output"],
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            conversation_debug_root=existing_debug_root,
        ),
        preflight_func=fake_preflight,
        run_func=fake_run,
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["status"] == DailyRunStatus.SUCCESS.value
    assert captured_config is not None
    assert captured_config.conversation_debug_root == existing_debug_root


def test_cli_merge_collected_returns_structured_json(capsys, tmp_path) -> None:
    def fake_run(*, target_date, config):
        return CollectedMergeRunResult(
            status=DailyRunStatus.SUCCESS.value,
            target_date=target_date,
            input_dir=str(tmp_path / "merge_inbox/2026/06/29"),
            output_path=str(
                tmp_path / "merge_inbox/2026/06/29/2026-06-29-管理者-merged.md"
            ),
            source_file_count=2,
            source_event_count=3,
            merged_event_count=2,
            skipped_file_count=0,
            warning_messages=[],
            self_delivery_status="success",
            self_delivery_target="ou_manager",
            self_delivery_error="",
            outputs=[
                CollectedMergeOutput(
                    input_dir=str(tmp_path / "merge_inbox/2026/06/29/项目A"),
                    output_path=str(
                        tmp_path
                        / "merge_inbox/2026/06/29/项目A/2026-06-29-管理者-merged.md"
                    ),
                    source_file_count=1,
                    source_event_count=1,
                    merged_event_count=1,
                    skipped_file_count=0,
                    warning_messages=[],
                    self_delivery_status="success",
                )
            ],
        )

    exit_code = main(
        ["merge-collected", "--date", "2026-06-29"],
        config=RuntimeConfig(data_root=tmp_path / "data"),
        collected_run_func=fake_run,
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["target_date"] == "2026-06-29"
    assert payload["source_file_count"] == 2
    assert payload["self_delivery_status"] == "success"
    assert payload["outputs"][0]["source_event_count"] == 1
