from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import logging
from time import perf_counter

from .analyzers.prompts import build_anchor_analysis_prompt, build_anchor_expansion_prompt
from .analyzers.protocol import parse_anchor_analysis_payload
from .cache import FileSystemAnchorCacheStore, build_anchor_input_fingerprint
from .config import RuntimeConfig
from .constants import AnchorStatus, DailyRunStatus
from .errors import AnalyzerProtocolError, ChatSourceError
from .factories import build_runtime_dependencies
from .models import AnchorUnit, AnchorAnalysisResult, AnchorCacheEntry, AttachmentTextBlock
from .logging_utils import log_timing
from .pipeline.anchor_expansion import expand_anchor_unit_context
from .pipeline.anchors import group_anchor_units
from .pipeline.filtering import filter_messages
from .utils.dates import now_iso
from .utils.json_io import dump_json

logger = logging.getLogger("worktrace")


@dataclass(frozen=True)
class AnchorExperimentResult:
    target_date: str
    status: str
    conversation_count: int
    message_count: int
    anchor_unit_count: int
    analyzed_anchor_count: int
    status_counts: dict[str, int]
    cache_bypass_enabled: bool
    cache_refresh_count: int
    cache_hit_count: int
    cache_miss_count: int
    completion_mode_counts: dict[str, int]
    cross_anchor_merge_count: int
    context_request_count: int
    candidate_event_count: int
    results_summary: list[dict]
    results: list[dict]
    error_summary: str

    def to_dict(self) -> dict[str, object]:
        return {
            "target_date": self.target_date,
            "status": self.status,
            "conversation_count": self.conversation_count,
            "message_count": self.message_count,
            "anchor_unit_count": self.anchor_unit_count,
            "analyzed_anchor_count": self.analyzed_anchor_count,
            "status_counts": dict(self.status_counts),
            "cache_bypass_enabled": self.cache_bypass_enabled,
            "cache_refresh_count": self.cache_refresh_count,
            "cache_hit_count": self.cache_hit_count,
            "cache_miss_count": self.cache_miss_count,
            "completion_mode_counts": dict(self.completion_mode_counts),
            "cross_anchor_merge_count": self.cross_anchor_merge_count,
            "context_request_count": self.context_request_count,
            "candidate_event_count": self.candidate_event_count,
            "results_summary": list(self.results_summary),
            "results": list(self.results),
            "error_summary": self.error_summary,
        }


def run_anchor_experiment(
    *,
    target_date: str,
    config: RuntimeConfig,
    limit: int | None = None,
    dump_dir: Path | None = None,
    ignore_cache: bool = False,
    refresh_cache: bool = False,
    runtime=None,
) -> AnchorExperimentResult:
    run_started_at = perf_counter()
    runtime = runtime or build_runtime_dependencies(config)

    try:
        stage_started_at = perf_counter()
        self_identity = runtime.chat_source.get_self_identity()
        log_timing(
            logger,
            "anchor_experiment.stage.completed",
            stage_started_at,
            stage="get_self_identity",
            target_date=target_date,
        )
        stage_started_at = perf_counter()
        conversations = runtime.chat_source.list_target_conversations(target_date, self_identity)
        log_timing(
            logger,
            "anchor_experiment.stage.completed",
            stage_started_at,
            stage="list_target_conversations",
            target_date=target_date,
            conversation_count=len(conversations),
        )
        stage_started_at = perf_counter()
        messages = runtime.chat_source.fetch_conversation_messages(
            target_date,
            [item.conversation_id for item in conversations],
        )
        log_timing(
            logger,
            "anchor_experiment.stage.completed",
            stage_started_at,
            stage="fetch_conversation_messages",
            target_date=target_date,
            message_count=len(messages),
        )
    except ChatSourceError as exc:
        return AnchorExperimentResult(
            target_date=target_date,
            status=DailyRunStatus.FAILED.value,
            conversation_count=0,
            message_count=0,
            anchor_unit_count=0,
            analyzed_anchor_count=0,
            status_counts={},
            cache_bypass_enabled=ignore_cache or refresh_cache,
            cache_refresh_count=0,
            cache_hit_count=0,
            cache_miss_count=0,
            completion_mode_counts={},
            cross_anchor_merge_count=0,
            context_request_count=0,
            candidate_event_count=0,
            results_summary=[],
            results=[],
            error_summary=str(exc),
        )

    stage_started_at = perf_counter()
    filtered_messages = filter_messages(messages)
    log_timing(
        logger,
        "anchor_experiment.stage.completed",
        stage_started_at,
        stage="filter_messages",
        input_message_count=len(messages),
        output_message_count=len(filtered_messages),
    )
    stage_started_at = perf_counter()
    anchor_units = group_anchor_units(filtered_messages, self_identity.open_id, config)
    log_timing(
        logger,
        "anchor_experiment.stage.completed",
        stage_started_at,
        stage="group_anchor_units",
        anchor_unit_count=len(anchor_units),
    )
    selected_units = anchor_units[:limit] if limit is not None else anchor_units
    cache_store = FileSystemAnchorCacheStore(config.cache_root or (config.data_root / "cache"))
    cache_refresh_count = 0
    if refresh_cache:
        stage_started_at = perf_counter()
        cache_refresh_count = cache_store.invalidate_day(target_date)
        log_timing(
            logger,
            "anchor_experiment.stage.completed",
            stage_started_at,
            stage="refresh_cache",
            target_date=target_date,
            cache_refresh_count=cache_refresh_count,
        )

    results: list[dict] = []
    try:
        batch_payloads = _analyze_anchor_units_first_pass_batched(
            runtime,
            target_date=target_date,
            anchor_units=selected_units,
            config=config,
            dump_dir=dump_dir,
            cache_store=cache_store,
            allow_cache_read=not (ignore_cache or refresh_cache),
        )
        for payload in batch_payloads:
            results.append(payload)
            log_timing(
                logger,
                "anchor_experiment.anchor.completed",
                perf_counter(),
                anchor_unit_id=payload["anchor_unit"]["anchor_unit_id"],
                cache_hit=payload.get("cache_hit") is True,
                pass_count=payload.get("pass_count", 0),
                completion_mode=payload.get("completion_mode", ""),
            )
    except AnalyzerProtocolError as exc:
        return AnchorExperimentResult(
            target_date=target_date,
            status=DailyRunStatus.FAILED.value,
            conversation_count=len(conversations),
            message_count=len(messages),
            anchor_unit_count=len(anchor_units),
            analyzed_anchor_count=len(results),
            status_counts=_count_anchor_statuses(results),
            cache_bypass_enabled=ignore_cache or refresh_cache,
            cache_refresh_count=cache_refresh_count,
            cache_hit_count=_count_cache_hits(results),
            cache_miss_count=_count_cache_misses(results),
            completion_mode_counts=_count_completion_modes(results),
            cross_anchor_merge_count=_count_cross_anchor_merges(results),
            context_request_count=_count_context_requests(results),
            candidate_event_count=_count_candidate_events(results),
            results_summary=_build_results_summary(results),
            results=results,
            error_summary=str(exc),
        )

    status_counts = _count_anchor_statuses(results)
    log_timing(
        logger,
        "anchor_experiment.run.completed",
        run_started_at,
        target_date=target_date,
        anchor_unit_count=len(anchor_units),
        analyzed_anchor_count=len(results),
        cache_hit_count=_count_cache_hits(results),
        cache_miss_count=_count_cache_misses(results),
    )
    return AnchorExperimentResult(
        target_date=target_date,
        status=DailyRunStatus.SUCCESS.value,
        conversation_count=len(conversations),
        message_count=len(messages),
        anchor_unit_count=len(anchor_units),
        analyzed_anchor_count=len(results),
        status_counts=status_counts,
        cache_bypass_enabled=ignore_cache or refresh_cache,
        cache_refresh_count=cache_refresh_count,
        cache_hit_count=_count_cache_hits(results),
        cache_miss_count=_count_cache_misses(results),
        completion_mode_counts=_count_completion_modes(results),
        cross_anchor_merge_count=_count_cross_anchor_merges(results),
        context_request_count=_count_context_requests(results),
        candidate_event_count=_count_candidate_events(results),
        results_summary=_build_results_summary(results),
        results=results,
        error_summary="",
    )


def _analyze_anchor_unit(
    runtime,
    *,
    target_date: str,
    anchor_unit: AnchorUnit,
    config: RuntimeConfig,
    dump_dir: Path | None = None,
    cache_store=None,
    allow_cache_read: bool = True,
) -> dict[str, object]:
    analyzer = runtime.analyzer

    current_anchor_unit = anchor_unit
    current_result: AnchorAnalysisResult | None = None
    attachment_texts: list[AttachmentTextBlock] = []
    pass_records: list[dict[str, object]] = []
    cache_hit = False
    cache_key: str | None = None
    cache_entry = None

    if cache_store is not None and allow_cache_read:
        cache_key = build_anchor_input_fingerprint(current_anchor_unit)
        cache_entry = cache_store.read(
            target_date=target_date,
            anchor_unit_id=current_anchor_unit.anchor_unit_id,
            input_fingerprint=cache_key,
        )
        if cache_entry is not None:
            cache_hit = True
            current_result = AnchorAnalysisResult(
                anchor_status=cache_entry.status,
                candidate_events=list(cache_entry.candidate_events),
                context_requests=list(cache_entry.context_requests),
                needs_cross_anchor_merge=cache_entry.needs_cross_anchor_merge,
            )
            pass_records.append(
                {
                    "pass_index": cache_entry.pass_index,
                    "analysis": current_result.to_dict(),
                    "expanded_message_ids": [],
                    "expanded_attachment_ids": list(cache_entry.included_attachment_ids),
                    "cache_hit": True,
                }
            )
            return {
                "anchor_unit": current_anchor_unit.to_dict(),
                "analysis": current_result.to_dict(),
                "pass_count": len(pass_records),
                "passes": pass_records,
                "attachment_texts": [],
                "cache_hit": True,
                "cache_key": cache_key,
                "completion_mode": "cache_hit",
            }

    for pass_index in range(1, config.anchor_retry_limit + 1):
        if pass_index == 1:
            prompt = build_anchor_analysis_prompt(
                target_date,
                current_anchor_unit,
                pass_index=pass_index,
                config=config,
            )
            new_messages = []
            new_attachment_texts = []
            trigger_requests = []
        else:
            assert current_result is not None
            trigger_requests = list(current_result.context_requests)
            (
                current_anchor_unit,
                new_messages,
                attachment_texts,
                new_attachment_texts,
            ) = expand_anchor_unit_context(
                current_anchor_unit,
                trigger_requests,
                chat_source=runtime.chat_source,
                content_resolver=runtime.content_resolver,
                config=config,
                existing_attachment_texts=attachment_texts,
            )
            prompt = build_anchor_expansion_prompt(
                target_date,
                current_anchor_unit,
                current_result,
                trigger_requests=trigger_requests,
                new_messages=new_messages,
                attachment_texts=new_attachment_texts,
                pass_index=pass_index,
                config=config,
            )

        if dump_dir is not None:
            _dump_anchor_debug_artifacts(
                dump_dir,
                anchor_unit=current_anchor_unit,
                prompt=prompt,
                target_date=target_date,
                stage="before",
                pass_index=pass_index,
                attachment_texts=attachment_texts,
                expansion_requests=trigger_requests,
                expansion_messages=new_messages,
                expansion_attachment_texts=new_attachment_texts,
            )
        payload = _invoke_anchor_analyzer(analyzer, prompt)
        parsed = parse_anchor_analysis_payload(payload)
        current_result = parsed
        cache_key = build_anchor_input_fingerprint(
            current_anchor_unit,
            attachment_texts=attachment_texts,
        )
        pass_records.append(
            {
                "pass_index": pass_index,
                "analysis": parsed.to_dict(),
                "expanded_message_ids": [item.message_id for item in new_messages],
                "expanded_attachment_ids": [item.attachment_id for item in new_attachment_texts],
                "cache_hit": False,
            }
        )
        if dump_dir is not None:
            _dump_anchor_debug_artifacts(
                dump_dir,
                anchor_unit=current_anchor_unit,
                prompt=prompt,
                target_date=target_date,
                stage="after",
                pass_index=pass_index,
                output_payload=parsed.to_dict(),
            )
        if _is_anchor_analysis_terminal(parsed):
            break

    if current_result is None:
        raise AnalyzerProtocolError("Anchor experiment produced no analysis result.")
    if cache_store is not None and cache_key is not None:
        cache_store.write(
            AnchorCacheEntry(
                target_date=target_date,
                anchor_unit_id=current_anchor_unit.anchor_unit_id,
                input_fingerprint=cache_key,
                status=current_result.anchor_status,
                pass_index=len(pass_records),
                prompt_version="v1",
                schema_version="v1",
                analyzer_key="codex",
                candidate_events=list(current_result.candidate_events),
                context_requests=list(current_result.context_requests),
                included_message_ids=[item.message_id for item in current_anchor_unit.messages],
                included_attachment_ids=[item.attachment_id for item in attachment_texts],
                needs_cross_anchor_merge=current_result.needs_cross_anchor_merge,
                created_at=now_iso(config.timezone),
            )
        )
    return {
        "anchor_unit": current_anchor_unit.to_dict(),
        "analysis": current_result.to_dict(),
        "pass_count": len(pass_records),
        "passes": pass_records,
        "attachment_texts": [item.to_dict() for item in attachment_texts],
        "cache_hit": cache_hit,
        "cache_key": cache_key,
        "completion_mode": _determine_completion_mode(
            current_result,
            pass_count=len(pass_records),
            cache_hit=cache_hit,
        ),
    }


def _analyze_anchor_units_first_pass_batched(
    runtime,
    *,
    target_date: str,
    anchor_units: list[AnchorUnit],
    config: RuntimeConfig,
    dump_dir: Path | None = None,
    cache_store=None,
    allow_cache_read: bool = True,
) -> list[dict[str, object]]:
    ready_results: list[dict[str, object]] = []
    pending_units: list[AnchorUnit] = []

    for anchor_unit in anchor_units:
        payload = _try_restore_anchor_from_cache(
            target_date=target_date,
            anchor_unit=anchor_unit,
            config=config,
            cache_store=cache_store,
            allow_cache_read=allow_cache_read,
        )
        if payload is not None:
            ready_results.append(payload)
        else:
            pending_units.append(anchor_unit)

    logger.info(
        "anchor_experiment.first_pass.cache_scan total_anchor_count=%s cache_hit_count=%s pending_anchor_count=%s allow_cache_read=%s",
        len(anchor_units),
        len(ready_results),
        len(pending_units),
        allow_cache_read,
    )

    if not pending_units:
        return ready_results

    batch_size = max(config.anchor_batch_size, 1)
    for index in range(0, len(pending_units), batch_size):
        chunk = pending_units[index : index + batch_size]
        logger.info(
            "anchor_experiment.first_pass.batch_dispatch batch_index=%s chunk_size=%s pending_anchor_count=%s total_anchor_count=%s",
            (index // batch_size) + 1,
            len(chunk),
            len(pending_units),
            len(anchor_units),
        )
        try:
            payload_map = _invoke_anchor_batch_first_pass(
                runtime,
                target_date=target_date,
                anchor_units=chunk,
                config=config,
                dump_dir=dump_dir,
            )
        except AnalyzerProtocolError:
            payload_map = {}

        for anchor_unit in chunk:
            payload = payload_map.get(anchor_unit.anchor_unit_id)
            if payload is not None:
                _write_anchor_cache_from_payload(
                    target_date=target_date,
                    payload=payload,
                    config=config,
                    cache_store=cache_store,
                )
                ready_results.append(payload)
                continue

            ready_results.append(
                _analyze_anchor_unit(
                    runtime,
                    target_date=target_date,
                    anchor_unit=anchor_unit,
                    config=config,
                    dump_dir=dump_dir,
                    cache_store=cache_store,
                    allow_cache_read=False,
                )
            )

    result_index = {item["anchor_unit"]["anchor_unit_id"]: item for item in ready_results}
    return [result_index[anchor_unit.anchor_unit_id] for anchor_unit in anchor_units]


def _try_restore_anchor_from_cache(
    *,
    target_date: str,
    anchor_unit: AnchorUnit,
    config: RuntimeConfig,
    cache_store,
    allow_cache_read: bool,
) -> dict[str, object] | None:
    if cache_store is None or not allow_cache_read:
        return None
    cache_key = build_anchor_input_fingerprint(anchor_unit)
    cache_entry = cache_store.read(
        target_date=target_date,
        anchor_unit_id=anchor_unit.anchor_unit_id,
        input_fingerprint=cache_key,
    )
    if cache_entry is None:
        return None
    current_result = AnchorAnalysisResult(
        anchor_status=cache_entry.status,
        candidate_events=list(cache_entry.candidate_events),
        context_requests=list(cache_entry.context_requests),
        needs_cross_anchor_merge=cache_entry.needs_cross_anchor_merge,
    )
    return {
        "anchor_unit": anchor_unit.to_dict(),
        "analysis": current_result.to_dict(),
        "pass_count": 1,
        "passes": [
            {
                "pass_index": cache_entry.pass_index,
                "analysis": current_result.to_dict(),
                "expanded_message_ids": [],
                "expanded_attachment_ids": list(cache_entry.included_attachment_ids),
                "cache_hit": True,
            }
        ],
        "attachment_texts": [],
        "cache_hit": True,
        "cache_key": cache_key,
        "completion_mode": "cache_hit",
    }


def _invoke_anchor_batch_first_pass(
    runtime,
    *,
    target_date: str,
    anchor_units: list[AnchorUnit],
    config: RuntimeConfig,
    dump_dir: Path | None = None,
) -> dict[str, dict[str, object]]:
    if not anchor_units:
        return {}
    analyzer = runtime.analyzer
    if not hasattr(analyzer, "analyze_anchor_batch"):
        raise AnalyzerProtocolError("Analyzer does not support batched anchor analysis.")
    result = analyzer.analyze_anchor_batch(target_date, anchor_units)
    mapped: dict[str, dict[str, object]] = {}
    anchor_by_id = {item.anchor_unit_id: item for item in anchor_units}
    for item in result.results:
        anchor_unit = anchor_by_id.get(item.anchor_unit_id)
        if anchor_unit is None:
            continue
        prompt = build_anchor_analysis_prompt(
            target_date,
            anchor_unit,
            pass_index=1,
            config=config,
        )
        if dump_dir is not None:
            _dump_anchor_debug_artifacts(
                dump_dir,
                anchor_unit=anchor_unit,
                prompt=prompt,
                target_date=target_date,
                stage="before",
                pass_index=1,
            )
            _dump_anchor_debug_artifacts(
                dump_dir,
                anchor_unit=anchor_unit,
                prompt=prompt,
                target_date=target_date,
                stage="after",
                pass_index=1,
                output_payload=item.analysis.to_dict(),
            )
        mapped[item.anchor_unit_id] = {
            "anchor_unit": anchor_unit.to_dict(),
            "analysis": item.analysis.to_dict(),
            "pass_count": 1,
            "passes": [
                {
                    "pass_index": 1,
                    "analysis": item.analysis.to_dict(),
                    "expanded_message_ids": [],
                    "expanded_attachment_ids": [],
                    "cache_hit": False,
                }
            ],
            "attachment_texts": [],
            "cache_hit": False,
            "cache_key": build_anchor_input_fingerprint(anchor_unit),
            "completion_mode": _determine_completion_mode(
                item.analysis,
                pass_count=1,
                cache_hit=False,
            ),
        }
    return mapped


def _write_anchor_cache_from_payload(
    *,
    target_date: str,
    payload: dict[str, object],
    config: RuntimeConfig,
    cache_store,
) -> None:
    if cache_store is None:
        return
    anchor_unit_payload = payload.get("anchor_unit")
    analysis_payload = payload.get("analysis")
    cache_key = payload.get("cache_key")
    if not isinstance(anchor_unit_payload, dict) or not isinstance(analysis_payload, dict):
        return
    if not isinstance(cache_key, str) or not cache_key:
        return
    analysis = AnchorAnalysisResult.from_dict(analysis_payload)
    anchor_unit = AnchorUnit.from_dict(anchor_unit_payload)
    cache_store.write(
        AnchorCacheEntry(
            target_date=target_date,
            anchor_unit_id=anchor_unit.anchor_unit_id,
            input_fingerprint=cache_key,
            status=analysis.anchor_status,
            pass_index=int(payload.get("pass_count", 1)),
            prompt_version="v1",
            schema_version="v1",
            analyzer_key="codex",
            candidate_events=list(analysis.candidate_events),
            context_requests=list(analysis.context_requests),
            included_message_ids=[item.message_id for item in anchor_unit.messages],
            included_attachment_ids=[],
            needs_cross_anchor_merge=analysis.needs_cross_anchor_merge,
            created_at=now_iso(config.timezone),
        )
    )


def _invoke_anchor_analyzer(analyzer, prompt: str) -> object:
    for method_name in ("_invoke_codex", "_invoke_hook"):
        method = getattr(analyzer, method_name, None)
        if callable(method):
            return method(prompt)
    raise AnalyzerProtocolError(
        "Anchor experiment requires an analyzer with a raw prompt invocation method."
    )


def render_anchor_experiment_json(
    result: AnchorExperimentResult,
    *,
    summary_only: bool = False,
) -> str:
    payload = result.to_dict()
    if summary_only:
        payload.pop("results", None)
    return dump_json(payload, pretty=True)


def render_anchor_experiment_summary_table(result: AnchorExperimentResult) -> str:
    headers = [
        "anchor_unit_id",
        "mode",
        "cache",
        "passes",
        "status",
        "events",
        "requests",
        "cross_merge",
    ]
    rows = [
        [
            str(item.get("anchor_unit_id", "")),
            str(item.get("completion_mode", "")),
            "Y" if item.get("cache_hit") is True else "N",
            str(item.get("pass_count", 0)),
            str(item.get("anchor_status", "")),
            str(item.get("candidate_event_count", 0)),
            str(item.get("context_request_count", 0)),
            "Y" if item.get("needs_cross_anchor_merge") is True else "N",
        ]
        for item in result.results_summary
    ]
    return _render_text_table(headers, rows)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    return _main_impl(argv, sys.stdout.write)


def _main_impl(
    argv: list[str] | None,
    write_output,
    *,
    config: RuntimeConfig = RuntimeConfig(),
    preflight_func=None,
    run_func=None,
) -> int:
    import argparse

    from .cli import validate_target_date
    from .preflight import run_preflight_checks

    parser = argparse.ArgumentParser(
        prog="python -m src.worktrace.anchor_experiment",
        add_help=True,
    )
    parser.add_argument("--date", dest="target_date", required=True)
    parser.add_argument("--limit", dest="limit", type=int, required=False)
    parser.add_argument("--dump-dir", dest="dump_dir", required=False)
    parser.add_argument("--ignore-cache", dest="ignore_cache", action="store_true")
    parser.add_argument("--refresh-cache", dest="refresh_cache", action="store_true")
    parser.add_argument("--summary-only", dest="summary_only", action="store_true")
    parser.add_argument("--summary-table", dest="summary_table", action="store_true")
    args = parser.parse_args(argv)

    target_date = validate_target_date(args.target_date)
    preflight = preflight_func or run_preflight_checks
    runner = run_func or run_anchor_experiment
    report = preflight(config, cwd=Path.cwd())
    if not report.ok:
        result = AnchorExperimentResult(
            target_date=target_date,
            status=DailyRunStatus.FAILED.value,
            conversation_count=0,
            message_count=0,
            anchor_unit_count=0,
            analyzed_anchor_count=0,
            status_counts={},
            cache_bypass_enabled=args.ignore_cache or args.refresh_cache,
            cache_refresh_count=0,
            cache_hit_count=0,
            cache_miss_count=0,
            completion_mode_counts={},
            cross_anchor_merge_count=0,
            context_request_count=0,
            candidate_event_count=0,
            results_summary=[],
            results=[],
            error_summary=report.error_summary,
        )
        if args.summary_table:
            write_output(render_anchor_experiment_summary_table(result))
        else:
            write_output(render_anchor_experiment_json(result, summary_only=args.summary_only))
        write_output("\n")
        return 1

    result = runner(
        target_date=target_date,
        config=config,
        limit=args.limit,
        dump_dir=None if not args.dump_dir else Path(args.dump_dir),
        ignore_cache=args.ignore_cache,
        refresh_cache=args.refresh_cache,
    )
    if args.summary_table:
        write_output(render_anchor_experiment_summary_table(result))
    else:
        write_output(render_anchor_experiment_json(result, summary_only=args.summary_only))
    write_output("\n")
    return 0 if result.status == DailyRunStatus.SUCCESS.value else 1


def _dump_anchor_debug_artifacts(
    dump_dir: Path,
    *,
    anchor_unit: AnchorUnit,
    prompt: str,
    target_date: str,
    stage: str,
    pass_index: int = 1,
    output_payload: dict[str, object] | None = None,
    attachment_texts: list[AttachmentTextBlock] | None = None,
    expansion_requests: list | None = None,
    expansion_messages: list | None = None,
    expansion_attachment_texts: list[AttachmentTextBlock] | None = None,
) -> None:
    anchor_dir = dump_dir / target_date / _safe_anchor_dir_name(anchor_unit.anchor_unit_id)
    pass_dir = anchor_dir / f"pass_{pass_index:02d}"
    pass_dir.mkdir(parents=True, exist_ok=True)
    if stage == "before":
        (pass_dir / "input.json").write_text(
            dump_json(anchor_unit.to_dict(), pretty=True) + "\n",
            encoding="utf-8",
        )
        (pass_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        if attachment_texts:
            (pass_dir / "attachment_texts.json").write_text(
                dump_json([item.to_dict() for item in attachment_texts], pretty=True) + "\n",
                encoding="utf-8",
            )
        if pass_index > 1:
            expansion_payload = {
                "trigger_requests": [
                    item.to_dict() for item in (expansion_requests or []) if hasattr(item, "to_dict")
                ],
                "new_messages": [
                    item.to_dict() for item in (expansion_messages or []) if hasattr(item, "to_dict")
                ],
                "new_attachment_texts": [
                    item.to_dict() for item in (expansion_attachment_texts or [])
                ],
            }
            (pass_dir / "expansion.json").write_text(
                dump_json(expansion_payload, pretty=True) + "\n",
                encoding="utf-8",
            )
    if stage == "after" and output_payload is not None:
        (pass_dir / "output.json").write_text(
            dump_json(output_payload, pretty=True) + "\n",
            encoding="utf-8",
        )


def _safe_anchor_dir_name(anchor_unit_id: str) -> str:
    return anchor_unit_id.replace("/", "_").replace(":", "__")


def _is_anchor_analysis_terminal(result: AnchorAnalysisResult) -> bool:
    if result.anchor_status in {
        AnchorStatus.COMPLETED.value,
        AnchorStatus.NOT_WORK_RELATED.value,
    }:
        return True
    return not result.context_requests


def _count_anchor_statuses(results: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in results:
        analysis = item.get("analysis")
        if not isinstance(analysis, dict):
            continue
        status = analysis.get("anchor_status")
        if not isinstance(status, str) or not status:
            continue
        counts[status] = counts.get(status, 0) + 1
    return counts


def _count_cache_hits(results: list[dict]) -> int:
    return sum(1 for item in results if item.get("cache_hit") is True)


def _count_cache_misses(results: list[dict]) -> int:
    return sum(1 for item in results if item.get("cache_hit") is not True)


def _count_cross_anchor_merges(results: list[dict]) -> int:
    count = 0
    for item in results:
        analysis = item.get("analysis")
        if isinstance(analysis, dict) and analysis.get("needs_cross_anchor_merge") is True:
            count += 1
    return count


def _count_completion_modes(results: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in results:
        mode = item.get("completion_mode")
        if not isinstance(mode, str) or not mode:
            continue
        counts[mode] = counts.get(mode, 0) + 1
    return counts


def _count_context_requests(results: list[dict]) -> int:
    total = 0
    for item in results:
        analysis = item.get("analysis")
        if not isinstance(analysis, dict):
            continue
        requests = analysis.get("context_requests")
        if isinstance(requests, list):
            total += len(requests)
    return total


def _count_candidate_events(results: list[dict]) -> int:
    total = 0
    for item in results:
        analysis = item.get("analysis")
        if not isinstance(analysis, dict):
            continue
        candidates = analysis.get("candidate_events")
        if isinstance(candidates, list):
            total += len(candidates)
    return total


def _build_results_summary(results: list[dict]) -> list[dict]:
    summary: list[dict] = []
    for item in results:
        anchor_unit = item.get("anchor_unit")
        analysis = item.get("analysis")
        if not isinstance(anchor_unit, dict) or not isinstance(analysis, dict):
            continue
        summary.append(
            {
                "anchor_unit_id": anchor_unit.get("anchor_unit_id", ""),
                "completion_mode": item.get("completion_mode", ""),
                "cache_hit": item.get("cache_hit") is True,
                "pass_count": item.get("pass_count", 0),
                "anchor_status": analysis.get("anchor_status", ""),
                "candidate_event_count": len(analysis.get("candidate_events", []))
                if isinstance(analysis.get("candidate_events"), list)
                else 0,
                "context_request_count": len(analysis.get("context_requests", []))
                if isinstance(analysis.get("context_requests"), list)
                else 0,
                "needs_cross_anchor_merge": analysis.get("needs_cross_anchor_merge") is True,
            }
        )
    return summary


def _determine_completion_mode(
    result: AnchorAnalysisResult,
    *,
    pass_count: int,
    cache_hit: bool,
) -> str:
    if cache_hit:
        return "cache_hit"
    if result.anchor_status == AnchorStatus.NOT_WORK_RELATED.value:
        return "not_work_related"
    if pass_count <= 1 and result.anchor_status == AnchorStatus.COMPLETED.value:
        return "first_pass_completed"
    if pass_count > 1 and result.anchor_status == AnchorStatus.COMPLETED.value:
        return "multi_pass_completed"
    if pass_count > 1:
        return "multi_pass_unresolved"
    return "first_pass_unresolved"


def _render_text_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    def _format_row(values: list[str]) -> str:
        return " | ".join(value.ljust(widths[index]) for index, value in enumerate(values))

    lines = [_format_row(headers), "-+-".join("-" * width for width in widths)]
    lines.extend(_format_row(row) for row in rows)
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
