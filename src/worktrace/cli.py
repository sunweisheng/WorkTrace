from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from .config import DEFAULT_CONFIG, RuntimeConfig
from .constants import DailyRunStatus
from .errors import InvalidInputError
from .logging_utils import configure_logging
from .models import DailyRunResult
from .preflight import run_preflight_checks
from .runner import run_daily_trace
from .utils.json_io import dump_json


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m src.worktrace.cli", add_help=True)
    parser.add_argument("--date", dest="target_date", required=False)
    return parser.parse_args(argv)


def validate_target_date(raw_date: str | None) -> str:
    if not raw_date:
        raise InvalidInputError("Missing required --date in YYYY-MM-DD format.")

    parts = raw_date.split("-")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise InvalidInputError("Invalid date format. Expected YYYY-MM-DD.")

    year, month, day = (int(part) for part in parts)
    try:
        __import__("datetime").date(year, month, day)
    except ValueError as exc:
        raise InvalidInputError("Invalid date value. Expected YYYY-MM-DD.") from exc
    return raw_date


def build_invalid_input_result(target_date: str | None, error_summary: str) -> DailyRunResult:
    return DailyRunResult(
        target_date=target_date or "",
        conversation_count=0,
        message_count=0,
        slice_count=0,
        batch_count=0,
        event_count=0,
        skipped_slice_count=0,
        warning_count=0,
        status=DailyRunStatus.INVALID_INPUT.value,
        output_path=None,
        error_summary=error_summary,
    )


def build_failed_result(target_date: str, error_summary: str) -> DailyRunResult:
    return DailyRunResult(
        target_date=target_date,
        conversation_count=0,
        message_count=0,
        slice_count=0,
        batch_count=0,
        event_count=0,
        skipped_slice_count=0,
        warning_count=0,
        status=DailyRunStatus.FAILED.value,
        output_path=None,
        error_summary=error_summary,
    )


def run_target_day(
    *,
    target_date: str,
    config: RuntimeConfig,
) -> DailyRunResult:
    return run_daily_trace(target_date, config)


def execute(
    argv: list[str] | None = None,
    *,
    config: RuntimeConfig = DEFAULT_CONFIG,
    preflight_func=run_preflight_checks,
    run_func=run_target_day,
) -> tuple[DailyRunResult, int]:
    logger = configure_logging()

    try:
        args = parse_args(argv)
        target_date = validate_target_date(args.target_date)
    except InvalidInputError as exc:
        return build_invalid_input_result(
            None if argv is None else _extract_raw_date(argv), str(exc)
        ), 2

    logger.info("Starting WorkTrace run", extra={"target_date": target_date, "stage": "cli"})

    report = preflight_func(config, cwd=Path.cwd())
    if not report.ok:
        return build_failed_result(target_date, report.error_summary), 1

    result = run_func(target_date=target_date, config=replace(config))
    if result.status == DailyRunStatus.INVALID_INPUT.value:
        return result, 2
    if result.status == DailyRunStatus.FAILED.value:
        return result, 1
    return result, 0


def _extract_raw_date(argv: list[str]) -> str | None:
    for index, token in enumerate(argv):
        if token == "--date" and index + 1 < len(argv):
            return argv[index + 1]
    return None


def main(
    argv: list[str] | None = None,
    *,
    config: RuntimeConfig = DEFAULT_CONFIG,
    preflight_func=run_preflight_checks,
    run_func=run_target_day,
) -> int:
    result, exit_code = execute(
        argv,
        config=config,
        preflight_func=preflight_func,
        run_func=run_func,
    )
    sys.stdout.write(dump_json(result.to_dict(), pretty=True))
    sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
