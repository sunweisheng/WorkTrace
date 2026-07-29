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
    SupportReportReference,
)


def _fake_support_report(**_kwargs) -> SupportReportReference:
    return SupportReportReference(
        status="generated_with_llm",
        path="data/debug/support_reports/worktrace-support-12345678.md",
        llm_status="success",
        privacy_check="passed",
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
    assert "support_report" not in payload
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
    old_debug_file = (
        tmp_path
        / "data"
        / "debug"
        / "conversations"
        / "2026-06-22"
        / "old.json"
    )
    old_debug_file.parent.mkdir(parents=True)
    old_debug_file.write_text("{}", encoding="utf-8")

    def fake_preflight(config, *, cwd):
        from src.worktrace.preflight import PreflightReport

        nonlocal captured_config
        captured_config = config
        return PreflightReport(ok=True, details={"cwd": str(cwd)})

    def fake_run(*, target_date, config):
        nonlocal captured_config
        captured_config = config
        assert not old_debug_file.exists()
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
        support_report_func=_fake_support_report,
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["status"] == DailyRunStatus.SUCCESS.value
    assert captured_config is not None
    assert captured_config.conversation_debug_root == tmp_path / "data" / "debug" / "conversations"


def test_cli_debug_output_cleans_configured_debug_directory(capsys, tmp_path) -> None:
    existing_debug_root = tmp_path / "custom-debug"
    old_debug_file = existing_debug_root / "2026-06-22" / "old.json"
    old_debug_file.parent.mkdir(parents=True)
    old_debug_file.write_text("{}", encoding="utf-8")
    captured_config = None

    def fake_preflight(config, *, cwd):
        from src.worktrace.preflight import PreflightReport

        return PreflightReport(ok=True, details={"cwd": str(cwd)})

    def fake_run(*, target_date, config):
        nonlocal captured_config
        captured_config = config
        assert not old_debug_file.exists()
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
        support_report_func=_fake_support_report,
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["status"] == DailyRunStatus.SUCCESS.value
    assert captured_config is not None
    assert captured_config.conversation_debug_root == existing_debug_root


def test_cli_resume_preserves_existing_debug_directory(capsys, tmp_path) -> None:
    old_debug_file = (
        tmp_path
        / "data"
        / "debug"
        / "conversations"
        / "2026-06-22"
        / "old.json"
    )
    old_debug_file.parent.mkdir(parents=True)
    old_debug_file.write_text("{}", encoding="utf-8")

    def fake_preflight(config, *, cwd):
        from src.worktrace.preflight import PreflightReport

        return PreflightReport(ok=True, details={"cwd": str(cwd)})

    def fake_run(*, target_date, config):
        assert old_debug_file.exists()
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
        ["--date", "2026-06-22", "--debug-output", "--resume"],
        config=RuntimeConfig(data_root=tmp_path / "data"),
        preflight_func=fake_preflight,
        run_func=fake_run,
        support_report_func=_fake_support_report,
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["status"] == DailyRunStatus.SUCCESS.value
    assert old_debug_file.exists()


def test_cli_merge_collected_returns_structured_json(capsys, tmp_path) -> None:
    personal_debug_file = (
        tmp_path
        / "data"
        / "debug"
        / "conversations"
        / "2026-06-29"
        / "old.json"
    )
    personal_debug_file.parent.mkdir(parents=True)
    personal_debug_file.write_text("{}", encoding="utf-8")

    def fake_run(*, target_date, config, merge_owner_name, offline):
        assert config.collected_merge_trace_enabled is False
        assert merge_owner_name is None
        assert offline is False
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
    assert personal_debug_file.exists()


def test_cli_debug_output_enables_collected_merge_trace(capsys, tmp_path) -> None:
    captured_config = None
    trace_root = tmp_path / "custom-collected-trace"

    def fake_run(*, target_date, config, merge_owner_name, offline):
        nonlocal captured_config
        captured_config = config
        assert merge_owner_name is None
        assert offline is False
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
        )

    exit_code = main(
        ["--debug-output", "merge-collected", "--date", "2026-06-29"],
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            collected_merge_trace_enabled=False,
            collected_merge_trace_root=trace_root,
        ),
        collected_run_func=fake_run,
        support_report_func=_fake_support_report,
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["status"] == DailyRunStatus.SUCCESS.value
    assert captured_config is not None
    assert captured_config.collected_merge_trace_enabled is True
    assert captured_config.collected_merge_trace_root == trace_root
    assert captured_config.conversation_debug_root is None
    assert payload["support_report"]["status"] == "generated_with_llm"
    assert payload["support_report"]["privacy_check"] == "passed"


def test_cli_merge_collected_passes_offline_server_options(capsys, tmp_path) -> None:
    captured_options = None

    def fake_run(*, target_date, config, merge_owner_name, offline):
        nonlocal captured_options
        captured_options = (target_date, config, merge_owner_name, offline)
        return CollectedMergeRunResult(
            status=DailyRunStatus.SUCCESS.value,
            target_date=target_date,
            input_dir=str(tmp_path / "merge_inbox/2026/06/29"),
            output_path=str(
                tmp_path / "merge_inbox/2026/06/29/2026-06-29-服务器负责人-merged.md"
            ),
            source_file_count=1,
            source_event_count=1,
            merged_event_count=1,
            skipped_file_count=0,
            warning_messages=[],
            self_delivery_status="disabled",
            self_delivery_target="",
            self_delivery_error="",
        )

    exit_code = main(
        [
            "merge-collected",
            "--date",
            "2026-06-29",
            "--owner-name",
            "服务器负责人",
            "--offline",
        ],
        config=RuntimeConfig(data_root=tmp_path / "data"),
        collected_run_func=fake_run,
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert captured_options is not None
    assert captured_options[0] == "2026-06-29"
    assert captured_options[2:] == ("服务器负责人", True)
    assert payload["self_delivery_status"] == "disabled"


def test_cli_rejects_offline_merge_without_owner_name(capsys) -> None:
    exit_code = main(
        ["merge-collected", "--date", "2026-06-29", "--offline"],
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 2
    assert payload["status"] == DailyRunStatus.INVALID_INPUT.value
    assert payload["error_summary"] == "Offline merge requires a non-empty --owner-name."


def test_cli_debug_preflight_failure_still_generates_support_report(
    capsys, tmp_path
) -> None:
    report_calls: list[dict[str, object]] = []

    def fake_preflight(config, *, cwd):
        from src.worktrace.preflight import PreflightReport

        return PreflightReport(
            ok=False,
            error_summary="Missing online LLM configuration",
        )

    def fake_report(**kwargs):
        report_calls.append(kwargs)
        return _fake_support_report(**kwargs)

    exit_code = main(
        ["--date", "2026-06-22", "--debug-output"],
        config=RuntimeConfig(data_root=tmp_path / "data"),
        preflight_func=fake_preflight,
        support_report_func=fake_report,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["status"] == DailyRunStatus.FAILED.value
    assert payload["support_report"]["status"] == "generated_with_llm"
    assert len(report_calls) == 1
    assert report_calls[0]["run_mode"] == "personal"


def test_cli_support_report_failure_does_not_change_run_exit_status(
    capsys, tmp_path
) -> None:
    def fake_preflight(config, *, cwd):
        from src.worktrace.preflight import PreflightReport

        return PreflightReport(ok=True)

    def fake_run(*, target_date, config):
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
            output_path=str(tmp_path / "daily.md"),
            error_summary="",
        )

    def failed_report(**_kwargs):
        raise OSError("report failed")

    exit_code = main(
        ["--date", "2026-06-22", "--debug-output"],
        config=RuntimeConfig(data_root=tmp_path / "data"),
        preflight_func=fake_preflight,
        run_func=fake_run,
        support_report_func=failed_report,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == DailyRunStatus.SUCCESS.value
    assert payload["support_report"] == {
        "status": "failed",
        "path": None,
        "llm_status": "failed",
        "privacy_check": "not_run",
        "schema_version": 1,
    }


def test_cli_invalid_debug_request_is_blocked_without_report_file(capsys) -> None:
    exit_code = main(["--date", "not-a-date", "--debug-output"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["status"] == DailyRunStatus.INVALID_INPUT.value
    assert payload["support_report"]["status"] == "blocked"
    assert payload["support_report"]["path"] is None
