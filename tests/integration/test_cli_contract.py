from __future__ import annotations

import json

from src.worktrace.cli import main
from src.worktrace.config import RuntimeConfig
from src.worktrace.constants import DailyRunStatus
from src.worktrace.models import DailyRunResult


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
