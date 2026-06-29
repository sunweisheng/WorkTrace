from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from .config import (
    DEFAULT_CONFIG,
    RuntimeConfig,
    load_conversation_blacklist_overrides,
    load_runtime_config_overrides,
)
from .constants import DailyRunStatus
from .errors import InvalidInputError
from .logging_utils import configure_logging
from .models import DailyRunResult, PreflightResult
from .preflight import run_preflight_checks
from .runner import run_daily_trace
from .utils.json_io import dump_json


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m src.worktrace.cli", add_help=True)
    parser.add_argument("--date", dest="target_date", required=False)
    parser.add_argument("--preflight", dest="preflight_only", action="store_true")
    parser.add_argument("--debug-output", dest="debug_output", action="store_true")
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
        self_delivery_status="",
        self_delivery_target="",
        self_delivery_error="",
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
        self_delivery_status="",
        self_delivery_target="",
        self_delivery_error="",
    )


def run_target_day(
    *,
    target_date: str,
    config: RuntimeConfig,
) -> DailyRunResult:
    return run_daily_trace(target_date, config)


def apply_cli_overrides(config: RuntimeConfig, args: argparse.Namespace) -> RuntimeConfig:
    if not args.debug_output or config.conversation_debug_root is not None:
        return config
    return replace(
        config,
        conversation_debug_root=config.data_root / "debug" / "conversations",
    )


def execute(
    argv: list[str] | None = None,
    *,
    config: RuntimeConfig = DEFAULT_CONFIG,
    preflight_func=run_preflight_checks,
    run_func=run_target_day,
) -> tuple[DailyRunResult | PreflightResult, int]:
    logger = configure_logging()

    args = parse_args(argv)
    file_config = load_runtime_config_overrides(config, cwd=Path.cwd())
    blacklist_config = load_conversation_blacklist_overrides(file_config, cwd=Path.cwd())
    effective_config = apply_cli_overrides(blacklist_config, args)
    if args.preflight_only:
        report = preflight_func(effective_config, cwd=Path.cwd())
        result = PreflightResult(
            status="ok" if report.ok else "failed",
            error_summary=report.error_summary,
            details=report.details,
        )
        return result, 0 if report.ok else 1

    try:
        target_date = validate_target_date(args.target_date)
    except InvalidInputError as exc:
        return build_invalid_input_result(
            None if argv is None else _extract_raw_date(argv), str(exc)
        ), 2

    logger.info("Starting WorkTrace run", extra={"target_date": target_date, "stage": "cli"})

    report = preflight_func(effective_config, cwd=Path.cwd())
    if not report.ok:
        return build_failed_result(target_date, report.error_summary), 1

    result = run_func(target_date=target_date, config=replace(effective_config))
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
