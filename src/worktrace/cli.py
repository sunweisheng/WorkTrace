from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .config import (
    DEFAULT_CONFIG,
    RuntimeConfig,
    load_conversation_blacklist_overrides,
    load_runtime_config_overrides,
)
from .constants import DailyRunStatus
from .errors import AnalyzerProtocolError, InvalidInputError
from .logging_utils import configure_logging
from .models import CollectedMergeRunResult, DailyRunResult, PreflightResult
from .reaction_catalogs.base import ReactionCatalogSyncResult
from .reaction_catalog import ReactionCatalogError
from .preflight import run_preflight_checks
from .runner import run_daily_trace
from .pipeline.llm_checkpoints import clear_day_llm_checkpoints
from .utils.filenames import parse_worktrace_markdown_filename
from .utils.json_io import dump_json


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m src.worktrace.cli", add_help=True)
    subparsers = parser.add_subparsers(dest="command")
    merge_parser = subparsers.add_parser("merge-collected")
    merge_parser.add_argument("--date", dest="target_date", required=True)
    sync_parser = subparsers.add_parser("sync-reaction-catalog")
    sync_parser.add_argument("--source", default="feishu")
    parser.add_argument("--date", dest="target_date", required=False)
    parser.add_argument("--preflight", dest="preflight_only", action="store_true")
    parser.add_argument("--debug-output", dest="debug_output", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Keep incomplete LLM checkpoints and reuse matching completed calls.",
    )
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


def run_collected_merge(
    *,
    target_date: str,
    config: RuntimeConfig,
):
    from .collected_merge import CollectedMergeRunner

    return CollectedMergeRunner(config=config).run(target_date)


def run_sync_reaction_catalog(
    *,
    source_id: str,
    config: RuntimeConfig,
    cwd: Path,
) -> ReactionCatalogSyncResult:
    from .factories import ReactionCatalogProviderFactory

    return ReactionCatalogProviderFactory.create(source_id, config, cwd=cwd).synchronize()


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
    collected_run_func=run_collected_merge,
    sync_reaction_catalog_func: Callable[..., ReactionCatalogSyncResult] = run_sync_reaction_catalog,
) -> tuple[DailyRunResult | PreflightResult | CollectedMergeRunResult | ReactionCatalogSyncResult, int]:
    logger = configure_logging()

    args = parse_args(argv)
    file_config = load_runtime_config_overrides(config, cwd=Path.cwd())
    blacklist_config = load_conversation_blacklist_overrides(file_config, cwd=Path.cwd())
    effective_config = apply_cli_overrides(blacklist_config, args)
    if args.command == "sync-reaction-catalog":
        try:
            result = sync_reaction_catalog_func(
                source_id=args.source,
                config=replace(effective_config),
                cwd=Path.cwd(),
            )
        except (AnalyzerProtocolError, ReactionCatalogError, OSError, ValueError) as exc:
            return (
                ReactionCatalogSyncResult(
                    source_id=args.source,
                    entry_count=0,
                    catalog_path=Path(effective_config.reaction_catalogs_root) / f"{args.source}.json",
                    asset_dir=Path("config") / "assets" / "reactions" / args.source,
                    status="failed",
                    error_summary=str(exc),
                ),
                1,
            )
        return result, 0
    if args.command == "merge-collected":
        try:
            target_date = validate_target_date(args.target_date)
        except InvalidInputError as exc:
            return build_invalid_input_result(args.target_date, str(exc)), 2
        result = collected_run_func(target_date=target_date, config=replace(effective_config))
        if result.status == DailyRunStatus.INVALID_INPUT.value:
            return result, 2
        if result.status == DailyRunStatus.FAILED.value:
            return result, 1
        return result, 0

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

    if not args.resume:
        _clear_previous_personal_run(effective_config, target_date)

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


def _clear_previous_personal_run(config: RuntimeConfig, target_date: str) -> None:
    clear_day_llm_checkpoints(config, target_date)
    year, month, _day = target_date.split("-")
    output_dir = config.data_root / year / month
    if not output_dir.exists():
        return
    for path in output_dir.glob("*.md"):
        parsed = parse_worktrace_markdown_filename(path.name)
        if parsed.target_date == target_date and not parsed.is_merged:
            path.unlink()


def main(
    argv: list[str] | None = None,
    *,
    config: RuntimeConfig = DEFAULT_CONFIG,
    preflight_func=run_preflight_checks,
    run_func=run_target_day,
    collected_run_func=run_collected_merge,
    sync_reaction_catalog_func: Callable[..., ReactionCatalogSyncResult] = run_sync_reaction_catalog,
) -> int:
    result, exit_code = execute(
        argv,
        config=config,
        preflight_func=preflight_func,
        run_func=run_func,
        collected_run_func=collected_run_func,
        sync_reaction_catalog_func=sync_reaction_catalog_func,
    )
    sys.stdout.write(dump_json(result.to_dict(), pretty=True))
    sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
