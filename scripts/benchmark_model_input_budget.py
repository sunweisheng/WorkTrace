from __future__ import annotations

import argparse
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any, Callable, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.evaluate_event_generation_quality import evaluate_result, load_json_object
from src.worktrace.analyzers.codex import CodexAnalyzer
from src.worktrace.analyzers.failover import FailoverAnalyzer
from src.worktrace.analyzers.online import OnlineLLMAnalyzer
from src.worktrace.collected_merge import CollectedMergeRunner
from src.worktrace.config import (
    ModelInputBudgetSelection,
    RuntimeConfig,
    load_codex_llm_settings,
    load_online_llm_settings,
    load_runtime_config_overrides,
)
from src.worktrace.factories import RuntimeDependencies
from src.worktrace.llm_usage import LLMUsageRecorder
from src.worktrace.models import (
    CollectedSourceEvent,
    NormalizedMessage,
    PersonalFactItem,
    SourceBackedEventDraft,
    WorkEvent,
)
from src.worktrace.runner import (
    DailyTraceRunner,
    _estimate_day_merge_input_tokens,
    _pack_day_merge_candidates,
    run_daily_trace,
)


DEFAULT_THRESHOLDS = (7000, 12000, 16000, 20000, 24000)
DEFAULT_FALLBACK_THRESHOLDS = (7000, 20000)
BENCHMARK_TARGET_DATE = "2000-01-01"


class _UnusedDependency:
    pass


def _config_for_threshold(config: RuntimeConfig, threshold: int) -> RuntimeConfig:
    return replace(
        config,
        model_input_batch_target_tokens=threshold,
        model_input_budget_selection=replace(
            config.model_input_budget_selection,
            target_tokens=threshold,
        ),
        data_root=Path("data") / "debug" / "model_input_budget_benchmark" / "disabled",
        cache_root=None,
        conversation_debug_root=None,
        collected_merge_trace_enabled=False,
        self_delivery_enabled=False,
    )


def _build_route_analyzer(
    route: str,
    *,
    config: RuntimeConfig,
    cwd: Path,
    recorder: LLMUsageRecorder,
) -> FailoverAnalyzer:
    if route == "primary":
        analyzer = CodexAnalyzer(config=config, cwd=cwd, usage_recorder=recorder)
    elif route == "fallback":
        analyzer = OnlineLLMAnalyzer(config=config, cwd=cwd, usage_recorder=recorder)
    else:
        raise ValueError(f"Unsupported benchmark route: {route}")
    return FailoverAnalyzer(
        primary=analyzer,
        fallback=None,
        usage_recorder=recorder,
        primary_backend=route,
        fallback_backend="disabled",
        primary_request_retry_limit=(
            config.primary_request_retry_limit if route == "primary" else 0
        ),
    )


def _dataset_items(
    dataset: dict[str, Any],
    mode: str,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [
        (case, item)
        for case in dataset["cases"]
        if case["mode"] == mode
        for item in case["source_items"]
    ]


def _benchmark_source_metadata(dataset: dict[str, Any]) -> dict[str, str]:
    raw = dataset.get("benchmark_source_metadata")
    if not isinstance(raw, dict) or set(raw) != {"retention_detail"}:
        raise ValueError(
            "benchmark_source_metadata must contain only retention_detail."
        )
    retention_detail = raw["retention_detail"]
    if not isinstance(retention_detail, str) or not retention_detail.strip():
        raise ValueError(
            "benchmark_source_metadata.retention_detail must be non-empty."
        )
    return {"retention_detail": retention_detail.strip()}


def _expanded_benchmark_context(
    dataset: dict[str, Any],
    *,
    source_id: str,
    text: str,
) -> str:
    raw = dataset.get("benchmark_non_fact_context")
    if not isinstance(raw, dict) or set(raw) != {"repeat", "line_template"}:
        raise ValueError(
            "benchmark_non_fact_context must contain repeat and line_template."
        )
    repeat = raw["repeat"]
    template = raw["line_template"]
    if not isinstance(repeat, int) or isinstance(repeat, bool) or repeat <= 0:
        raise ValueError("benchmark_non_fact_context.repeat must be positive.")
    if not isinstance(template, str) or not template.strip():
        raise ValueError(
            "benchmark_non_fact_context.line_template must be non-empty."
        )
    padding_lines = _benchmark_padding_lines(
        source_id=source_id,
        repeat=repeat,
        template=template,
    )
    return "\n".join([text, *padding_lines])


def _benchmark_padding_lines(
    *,
    source_id: str,
    repeat: int,
    template: str,
) -> list[str]:
    padding_lines: list[str] = []
    try:
        for index in range(1, repeat + 1):
            payload = sha256(f"{source_id}:{index}".encode("utf-8")).hexdigest()
            padding_lines.append(
                template.format(
                    source_id=source_id,
                    index=index,
                    payload=payload,
                )
            )
    except (KeyError, ValueError) as exc:
        raise ValueError(
            "benchmark_non_fact_context.line_template uses an invalid placeholder."
        ) from exc
    return padding_lines


def _strip_benchmark_non_fact_context(
    dataset: dict[str, Any],
    text: str,
) -> str:
    raw = dataset["benchmark_non_fact_context"]
    padding_lines = {
        line
        for _case, item in _dataset_items(dataset, "personal")
        + _dataset_items(dataset, "collected")
        for line in _benchmark_padding_lines(
            source_id=item["source_id"],
            repeat=raw["repeat"],
            template=raw["line_template"],
        )
    }
    return "\n".join(
        line for line in text.splitlines() if line not in padding_lines
    ).strip()


def _build_personal_inputs(
    dataset: dict[str, Any],
    config: RuntimeConfig,
) -> tuple[list[SourceBackedEventDraft], list[NormalizedMessage]]:
    action_label = (
        config.action_label_types[0].key if config.action_label_types else "other"
    )
    source_metadata = _benchmark_source_metadata(dataset)
    candidates: list[SourceBackedEventDraft] = []
    messages: list[NormalizedMessage] = []
    for index, (case, item) in enumerate(
        _dataset_items(dataset, "personal"),
        start=1,
    ):
        source_id = item["source_id"]
        text = item["text"]
        expanded_text = _expanded_benchmark_context(
            dataset,
            source_id=source_id,
            text=text,
        )
        candidates.append(
            SourceBackedEventDraft(
                draft_id=source_id,
                date=BENCHMARK_TARGET_DATE,
                topic=text,
                content=expanded_text,
                source_message_ids=[source_id],
                source_conversation_id=case["case_id"],
                source_slice_id=source_id,
                confidence=1.0,
                action_label=action_label,
                object_hint=text,
                retention_reason="decision_made",
                retention_detail=source_metadata["retention_detail"],
                self_evidence_message_ids=[source_id],
                fact_items=[
                    PersonalFactItem(
                        field_name="content",
                        text=text,
                        evidence_message_ids=[source_id],
                    )
                ],
            )
        )
        messages.append(
            NormalizedMessage(
                conversation_id=case["case_id"],
                conversation_name="脱敏评测会话",
                message_id=source_id,
                sender_open_id=f"person-{index:03d}",
                sender_name=f"人员{index:03d}",
                send_time=f"{BENCHMARK_TARGET_DATE}T10:{index:02d}:00+08:00",
                message_type="text",
                text=text,
                reply_to_message_id=None,
                quote_message_id=None,
                links=[],
                attachments=[],
                is_system=False,
            )
        )
    return candidates, messages


def _build_collected_inputs(
    dataset: dict[str, Any],
    config: RuntimeConfig,
) -> list[CollectedSourceEvent]:
    source_metadata = _benchmark_source_metadata(dataset)
    action_labels = (
        [config.action_label_types[0].key]
        if config.action_label_types
        else []
    )
    self_relations = (
        [config.self_relation_types[0].key]
        if config.self_relation_types
        else []
    )
    events: list[CollectedSourceEvent] = []
    for index, (case, item) in enumerate(
        _dataset_items(dataset, "collected"),
        start=1,
    ):
        source_id = item["source_id"]
        text = item["text"]
        expanded_text = _expanded_benchmark_context(
            dataset,
            source_id=source_id,
            text=text,
        )
        person_name = f"人员{index:03d}"
        evidence_fingerprint = "sha256:" + sha256(
            source_id.encode("utf-8")
        ).hexdigest()
        conversation_fingerprint = "sha256:" + sha256(
            case["case_id"].encode("utf-8")
        ).hexdigest()
        events.append(
            CollectedSourceEvent(
                draft_id=source_id,
                person_name=person_name,
                source_file=f"source-{index:03d}.md",
                event=WorkEvent(
                    date=BENCHMARK_TARGET_DATE,
                    event_id=source_id,
                    title=text,
                    content=expanded_text,
                    source_message_ids=[source_id],
                    source_people=[person_name],
                    source_event_ids=[source_id],
                    object_hint=text,
                    retention_reason="decision_made",
                    retention_detail=source_metadata["retention_detail"],
                    action_labels=list(action_labels),
                    self_relations=list(self_relations),
                    evidence_fingerprints=[evidence_fingerprint],
                    conversation_fingerprints=[conversation_fingerprint],
                ),
            )
        )
    return events


def _record_metrics(
    records: list[dict[str, object]],
    *,
    threshold: int,
    validation_retry_count: int,
    wall_clock_ms: float,
    batch_fragment_counts: list[int],
) -> dict[str, object]:
    estimates = [
        int(value)
        for record in records
        for value in [record.get("estimated_input_tokens")]
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
    ]
    durations = [
        float(value)
        for record in records
        for value in [record.get("duration_ms")]
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    validation_statuses = [
        validation.get("status")
        for record in records
        for validation in [record.get("python_validation")]
        if isinstance(validation, dict)
    ]
    return {
        "batch_count": len(batch_fragment_counts),
        "batch_fragment_counts": batch_fragment_counts,
        "oversized_request_count": sum(
            estimate > threshold for estimate in estimates
        ),
        "input_estimated_tokens_total": sum(estimates),
        "input_estimated_tokens_max": max(estimates, default=0),
        "input_estimated_tokens_average": (
            round(mean(estimates), 3) if estimates else 0.0
        ),
        "model_call_count": len(records),
        "failed_request_count": sum(
            record.get("status") == "failed" for record in records
        ),
        "retry_count": validation_retry_count
        + sum(max(int(record.get("attempt", 1)) - 1, 0) for record in records),
        "fallback_count": sum(bool(record.get("fallback_to")) for record in records),
        "protocol_failure_count": sum(
            record.get("error_category")
            in {"invalid_protocol", "invalid_json", "empty_response", "stream_json"}
            for record in records
        ),
        "timeout_failure_count": sum(
            record.get("error_category") == "timeout" for record in records
        ),
        "function_validation_pass_count": sum(
            status == "passed" for status in validation_statuses
        ),
        "function_validation_checked_count": len(validation_statuses),
        "request_accumulated_ms": round(sum(durations), 3),
        "wall_clock_ms": round(wall_clock_ms, 3),
        "request_duration_ms": {
            "count": len(durations),
            "total": round(sum(durations), 3),
            "max": round(max(durations), 3) if durations else 0.0,
            "average": round(mean(durations), 3) if durations else 0.0,
        },
    }


def _personal_benchmark(
    dataset: dict[str, Any],
    *,
    config: RuntimeConfig,
    analyzer: FailoverAnalyzer,
    recorder: LLMUsageRecorder,
) -> dict[str, object]:
    candidates, messages = _build_personal_inputs(dataset, config)
    full_estimate = _estimate_day_merge_input_tokens(
        BENCHMARK_TARGET_DATE,
        candidates,
        config,
    )
    batches = (
        [candidates]
        if full_estimate <= config.model_input_batch_target_tokens
        else _pack_day_merge_candidates(
            target_date=BENCHMARK_TARGET_DATE,
            candidates=candidates,
            input_limit=config.model_input_batch_target_tokens,
            config=config,
        )
    )
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=_UnusedDependency(),
            content_resolver=_UnusedDependency(),
            analyzer=analyzer,
            delivery_channel=_UnusedDependency(),
            event_store=_UnusedDependency(),
            llm_usage_recorder=recorder,
        ),
        reaction_catalog=_UnusedDependency(),
    )
    started_at = perf_counter()
    validation_retry_count = 0
    python_validation_pass = True
    error_type = ""
    groups: list[dict[str, object]] = []
    try:
        (
            group_result,
            _grouping_warnings,
            grouping_attempts,
            grouping_retry_count,
            _fallback_count,
            singleton_repair_count,
        ) = runner._merge_day_candidates_with_batching(
            BENCHMARK_TARGET_DATE,
            candidates,
        )
        validation_retry_count += grouping_retry_count
        python_validation_pass = singleton_repair_count == 0 and all(
            attempt.get("status") == "success" for attempt in grouping_attempts
        )
        discovery = runner._discover_day_group_review_candidates(
            target_date=BENCHMARK_TARGET_DATE,
            groups=group_result.groups,
            candidates=candidates,
        )
        (
            reviewed_groups,
            _review_warnings,
            _review_attempts,
            _review_components,
            _review_component_count,
            _review_request_count,
            review_retry_count,
            _review_fallback_count,
            review_metrics,
        ) = runner._review_strongly_related_day_groups(
            target_date=BENCHMARK_TARGET_DATE,
            groups=group_result.groups,
            candidates=candidates,
            messages=messages,
            discovery_result=discovery.result,
        )
        validation_retry_count += review_retry_count + discovery.retry_count
        python_validation_pass = python_validation_pass and (
            discovery.failure_count == 0
            and review_metrics["review_failure_count"] == 0
        )
        render = runner._render_personal_multi_groups(
            target_date=BENCHMARK_TARGET_DATE,
            groups=reviewed_groups,
            candidates=candidates,
        )
        validation_retry_count += render.retry_count
        python_validation_pass = python_validation_pass and render.failure_count == 0
        candidate_by_id = {item.draft_id: item for item in candidates}
        draft_id_by_message_id = {
            message_id: item.draft_id
            for item in candidates
            for message_id in item.source_message_ids
        }
        for group in reviewed_groups:
            rendered = render.rendered_groups.get(group.group_id)
            primary = candidate_by_id[group.primary_draft_id]
            fact_source_ids = list(group.draft_ids)
            if rendered is not None:
                title = rendered.topic
                content = rendered.content
                fact_source_ids = list(
                    dict.fromkeys(
                        draft_id_by_message_id[message_id]
                        for fact in rendered.fact_items
                        for message_id in fact.evidence_message_ids
                        if message_id in draft_id_by_message_id
                    )
                )
            else:
                title = primary.topic
                content = primary.content
            groups.append(
                {
                    "member_ids": list(group.draft_ids),
                    "title": title,
                    "content": _strip_benchmark_non_fact_context(
                        dataset,
                        content,
                    ),
                    "fact_source_ids": fact_source_ids,
                }
            )
    except Exception as exc:
        python_validation_pass = False
        error_type = type(exc).__name__
    wall_clock_ms = (perf_counter() - started_at) * 1000
    records = recorder.records()
    return {
        "mode": "personal",
        "status": "success" if groups and not error_type else "failed",
        "error_type": error_type,
        "groups": groups,
        "python_validation_pass": python_validation_pass,
        **_record_metrics(
            records,
            threshold=config.model_input_batch_target_tokens,
            validation_retry_count=validation_retry_count,
            wall_clock_ms=wall_clock_ms,
            batch_fragment_counts=[len(batch) for batch in batches],
        ),
    }


def _collected_benchmark(
    dataset: dict[str, Any],
    *,
    config: RuntimeConfig,
    analyzer: FailoverAnalyzer,
    recorder: LLMUsageRecorder,
) -> dict[str, object]:
    events = _build_collected_inputs(dataset, config)
    runner = CollectedMergeRunner(
        config=config,
        analyzer=analyzer,
        cwd=Path.cwd(),
        delivery_channel=_UnusedDependency(),
        self_identity_resolver=lambda: None,
    )
    deterministic_groups, _warnings = runner._build_deterministic_groups(events)
    full_estimate = runner._estimate_collected_grouping_prompt_tokens(
        BENCHMARK_TARGET_DATE,
        events,
        deterministic_groups,
    )
    batches = (
        [events]
        if full_estimate <= config.model_input_batch_target_tokens
        else runner._pack_collected_grouping_batches(
            BENCHMARK_TARGET_DATE,
            events,
            deterministic_groups,
        )
    )
    started_at = perf_counter()
    error_type = ""
    groups: list[dict[str, object]] = []
    python_validation_pass = True
    try:
        merged_events, warnings = runner._merge_source_events_two_stage(
            BENCHMARK_TARGET_DATE,
            events,
        )
        python_validation_pass = bool(merged_events) and not any(
            marker in warning.lower()
            for warning in warnings
            for marker in ("failed", "invalid", "repaired", "filtered")
        )
        groups = [
            {
                "member_ids": list(event.source_event_ids),
                "title": event.title,
                "content": _strip_benchmark_non_fact_context(
                    dataset,
                    event.content,
                ),
                "fact_source_ids": list(event.source_event_ids),
            }
            for event in merged_events
        ]
    except Exception as exc:
        python_validation_pass = False
        error_type = type(exc).__name__
    wall_clock_ms = (perf_counter() - started_at) * 1000
    records = recorder.records()
    return {
        "mode": "collected",
        "status": "success" if groups and not error_type else "failed",
        "error_type": error_type,
        "groups": groups,
        "python_validation_pass": python_validation_pass,
        **_record_metrics(
            records,
            threshold=config.model_input_batch_target_tokens,
            validation_retry_count=0,
            wall_clock_ms=wall_clock_ms,
            batch_fragment_counts=[len(batch) for batch in batches],
        ),
    }


def _failed_groups_for_case(case: dict[str, Any]) -> list[dict[str, object]]:
    return [
        {
            "member_ids": [item["source_id"]],
            "title": "BENCHMARK_RUN_FAILED",
            "content": "BENCHMARK_RUN_FAILED",
            "fact_source_ids": [],
        }
        for item in case["source_items"]
    ]


def _case_results(
    dataset: dict[str, Any],
    mode_results: list[dict[str, object]],
) -> list[dict[str, object]]:
    mode_by_name = {str(result["mode"]): result for result in mode_results}
    first_case_seen: set[str] = set()
    results: list[dict[str, object]] = []
    for case in dataset["cases"]:
        mode = case["mode"]
        mode_result = mode_by_name[mode]
        source_ids = {item["source_id"] for item in case["source_items"]}
        groups = [
            dict(group)
            for group in mode_result["groups"]
            if source_ids.intersection(group["member_ids"])
        ]
        if not groups:
            groups = _failed_groups_for_case(case)
        owns_metrics = mode not in first_case_seen
        first_case_seen.add(mode)
        results.append(
            {
                "case_id": case["case_id"],
                "groups": groups,
                "input_estimated_tokens": (
                    int(mode_result["input_estimated_tokens_total"])
                    if owns_metrics
                    else 0
                ),
                "model_call_count": (
                    int(mode_result["model_call_count"]) if owns_metrics else 0
                ),
                "retry_count": int(mode_result["retry_count"]) if owns_metrics else 0,
                "elapsed_ms": (
                    round(float(mode_result["wall_clock_ms"])) if owns_metrics else 0
                ),
            }
        )
    return results


def run_threshold_once(
    dataset: dict[str, Any],
    *,
    config: RuntimeConfig,
    route: str,
    threshold: int,
    repeat_index: int,
    cwd: Path,
) -> dict[str, object]:
    route_config = _config_for_threshold(config, threshold)
    mode_results: list[dict[str, object]] = []
    for mode in ("personal", "collected"):
        recorder = LLMUsageRecorder()
        analyzer = _build_route_analyzer(
            route,
            config=route_config,
            cwd=cwd,
            recorder=recorder,
        )
        if mode == "personal":
            mode_results.append(
                _personal_benchmark(
                    dataset,
                    config=route_config,
                    analyzer=analyzer,
                    recorder=recorder,
                )
            )
        else:
            mode_results.append(
                _collected_benchmark(
                    dataset,
                    config=route_config,
                    analyzer=analyzer,
                    recorder=recorder,
                )
            )

    model = (
        load_codex_llm_settings(route_config, cwd=cwd).model
        if route == "primary"
        else load_online_llm_settings(route_config, cwd=cwd).model
    )
    result_payload = {
        "metadata": {
            "variant": f"threshold-{threshold}-{route}-repeat-{repeat_index}",
            "model": model,
            "service": route,
            "reasoning_effort": (
                load_codex_llm_settings(route_config, cwd=cwd).reasoning_effort
                if route == "primary"
                else str(
                    load_online_llm_settings(route_config, cwd=cwd).reasoning_effort
                    or "none"
                )
            ),
            "stream_enabled": (
                False
                if route == "primary"
                else load_online_llm_settings(route_config, cwd=cwd).stream_enabled
            ),
        },
        "cases": _case_results(dataset, mode_results),
    }
    automatic = evaluate_result(dataset, result_payload)
    return {
        "route": route,
        "threshold": threshold,
        "repeat_index": repeat_index,
        "mode_results": mode_results,
        "result_payload": result_payload,
        "automatic_evaluation": automatic,
    }


def _mode_automatic_summary(
    automatic: dict[str, Any],
    mode: str,
) -> dict[str, float]:
    cases = [case for case in automatic["cases"] if case["mode"] == mode]
    expected_sources = sum(case["expected_source_count"] for case in cases)
    covered_sources = sum(case["source_covered_count"] for case in cases)
    return {
        "source_coverage_rate": (
            covered_sources / expected_sources if expected_sources else 1.0
        ),
        "format_error_count": float(
            sum(
                case[key]
                for case in cases
                for key in (
                    "missing_source_assignment_count",
                    "duplicate_source_assignment_count",
                    "unknown_source_assignment_count",
                    "missing_fact_source_count",
                    "invalid_fact_source_reference_count",
                )
            )
        ),
        "incorrect_split_pair_count": float(
            sum(case["incorrect_split_pair_count"] for case in cases)
        ),
        "incorrect_merge_pair_count": float(
            sum(case["incorrect_merge_pair_count"] for case in cases)
        ),
    }


def _average_run_metric(
    runs: Iterable[dict[str, object]],
    mode: str,
    key: str,
) -> float:
    values = [
        float(mode_result[key])
        for run in runs
        for mode_result in run["mode_results"]
        if mode_result["mode"] == mode
    ]
    return mean(values) if values else 0.0


def automatic_gate(
    runs: list[dict[str, object]],
    baseline_runs: list[dict[str, object]],
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    for mode in ("personal", "collected"):
        summaries = [
            _mode_automatic_summary(run["automatic_evaluation"], mode)
            for run in runs
        ]
        baseline_summaries = [
            _mode_automatic_summary(run["automatic_evaluation"], mode)
            for run in baseline_runs
        ]
        if any(summary["source_coverage_rate"] != 1.0 for summary in summaries):
            errors.append(f"{mode}:source_coverage")
        if any(summary["format_error_count"] != 0 for summary in summaries):
            errors.append(f"{mode}:format_or_evidence")
        for key in ("incorrect_split_pair_count", "incorrect_merge_pair_count"):
            if mean(summary[key] for summary in summaries) > mean(
                summary[key] for summary in baseline_summaries
            ):
                errors.append(f"{mode}:{key}")
        for key in (
            "failed_request_count",
            "protocol_failure_count",
            "timeout_failure_count",
        ):
            if _average_run_metric(runs, mode, key) > _average_run_metric(
                baseline_runs,
                mode,
                key,
            ):
                errors.append(f"{mode}:{key}")
        if any(
            mode_result["status"] != "success"
            or not mode_result["python_validation_pass"]
            for run in runs
            for mode_result in run["mode_results"]
            if mode_result["mode"] == mode
        ):
            errors.append(f"{mode}:python_validation")
    return not errors, list(dict.fromkeys(errors))


def _blind_review_artifacts(
    runs: list[dict[str, object]],
    dataset: dict[str, Any],
) -> tuple[list[dict[str, object]], dict[str, dict[str, object]]]:
    review_items: list[dict[str, object]] = []
    mapping: dict[str, dict[str, object]] = {}
    for run in runs:
        automatic = run["automatic_evaluation"]
        for case in automatic["cases"]:
            seed = (
                f"{run['route']}:{run['threshold']}:{run['repeat_index']}:"
                f"{case['case_id']}"
            )
            review_id = "R-" + sha256(seed.encode("utf-8")).hexdigest()[:12]
            source_case = next(
                item
                for item in dataset["cases"]
                if item["case_id"] == case["case_id"]
            )
            result_case = next(
                item
                for item in automatic["cases"]
                if item["case_id"] == case["case_id"]
            )
            review_items.append(
                {
                    "review_id": review_id,
                    "mode": case["mode"],
                    "source_facts": [
                        item["text"] for item in source_case["source_items"]
                    ],
                    "generated_groups": [
                        {
                            "title": group["title"],
                            "content": group["content"],
                        }
                        for group in next(
                            item
                            for item in run["result_payload"]["cases"]
                            if item["case_id"] == case["case_id"]
                        )["groups"]
                    ],
                }
            )
            mapping[review_id] = {
                "route": run["route"],
                "threshold": run["threshold"],
                "repeat_index": run["repeat_index"],
                "mode": case["mode"],
                "case_id": case["case_id"],
            }
    review_items.sort(key=lambda item: item["review_id"])
    return review_items, mapping


def select_threshold(
    runs: list[dict[str, object]],
    manual_scores: dict[str, dict[str, object]],
    review_mapping: dict[str, dict[str, object]],
    *,
    eligible_thresholds: set[int] | None = None,
) -> dict[str, object]:
    scores_by_threshold: dict[int, list[dict[str, object]]] = {}
    baseline_averages: dict[tuple[str, str], tuple[float, float]] = {}
    for review_id, mapping in review_mapping.items():
        if review_id not in manual_scores:
            raise ValueError(f"Missing manual review score: {review_id}")
        score = manual_scores[review_id]
        if set(score) != {"fact_accurate", "completeness", "readability"}:
            raise ValueError(f"Invalid manual review fields: {review_id}")
        if not isinstance(score["fact_accurate"], bool):
            raise ValueError(f"fact_accurate must be boolean: {review_id}")
        for key in ("completeness", "readability"):
            value = score[key]
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 5:
                raise ValueError(f"{key} must be an integer from 1 to 5: {review_id}")
        scored = {**mapping, **score}
        scores_by_threshold.setdefault(int(mapping["threshold"]), []).append(scored)

    baseline_scores = scores_by_threshold.get(7000, [])
    for route in ("primary", "fallback"):
        for mode in ("personal", "collected"):
            matching = [
                item
                for item in baseline_scores
                if item["route"] == route and item["mode"] == mode
            ]
            if matching:
                baseline_averages[(route, mode)] = (
                    mean(float(item["completeness"]) for item in matching),
                    mean(float(item["readability"]) for item in matching),
                )

    eligible: list[dict[str, object]] = []
    for threshold, scores in sorted(scores_by_threshold.items()):
        if eligible_thresholds is not None and threshold not in eligible_thresholds:
            continue
        reasons: list[str] = []
        if not all(item["fact_accurate"] for item in scores):
            reasons.append("fact_accuracy")
        for route in ("primary", "fallback"):
            for mode in ("personal", "collected"):
                matching = [
                    item
                    for item in scores
                    if item["route"] == route and item["mode"] == mode
                ]
                baseline = baseline_averages.get((route, mode))
                if not matching or baseline is None:
                    reasons.append(f"missing_scores:{route}:{mode}")
                    continue
                if mean(float(item["completeness"]) for item in matching) < baseline[0]:
                    reasons.append(f"completeness:{route}:{mode}")
                if mean(float(item["readability"]) for item in matching) < baseline[1]:
                    reasons.append(f"readability:{route}:{mode}")
        run_subset = [run for run in runs if run["threshold"] == threshold]
        wall_clock_ms = sum(
            float(mode_result["wall_clock_ms"])
            for run in run_subset
            for mode_result in run["mode_results"]
        )
        item = {
            "threshold": threshold,
            "eligible": not reasons,
            "reasons": reasons,
            "completeness_average": mean(
                float(score["completeness"]) for score in scores
            ),
            "readability_average": mean(
                float(score["readability"]) for score in scores
            ),
            "wall_clock_ms": round(wall_clock_ms, 3),
        }
        if not reasons:
            eligible.append(item)

    if not eligible:
        return {"selected_target_tokens": 7000, "reason": "no_candidate_passed"}
    best_completeness = max(float(item["completeness_average"]) for item in eligible)
    quality_tied = [
        item
        for item in eligible
        if float(item["completeness_average"]) == best_completeness
    ]
    best_readability = max(float(item["readability_average"]) for item in quality_tied)
    quality_tied = [
        item
        for item in quality_tied
        if float(item["readability_average"]) == best_readability
    ]
    fastest = min(float(item["wall_clock_ms"]) for item in quality_tied)
    near_fastest = [
        item
        for item in quality_tied
        if fastest == 0
        or (float(item["wall_clock_ms"]) - fastest) / fastest < 0.10
    ]
    selected = min(near_fastest, key=lambda item: int(item["threshold"]))
    return {
        "selected_target_tokens": selected["threshold"],
        "reason": "quality_then_time_then_lower_threshold",
        "candidates": eligible,
    }


def _apply_profile_selection(
    *,
    config_path: Path,
    config: RuntimeConfig,
    target_tokens: int,
    benchmark_dataset_version: str,
) -> None:
    if not config.model_input_budget_selection.profile_matched:
        raise ValueError("Current model pair does not match a budget profile.")
    payload = load_json_object(config_path)
    profile_id = config.model_input_budget_selection.profile_id
    matched = 0
    for profile in payload["profiles"]:
        if profile["profile_id"] == profile_id:
            profile["target_tokens"] = target_tokens
            profile["benchmark_dataset_version"] = benchmark_dataset_version
            matched += 1
    if matched != 1:
        raise ValueError("Selected budget profile was not found exactly once.")
    config_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def run_isolated_date_validation(
    target_date: str,
    *,
    config: RuntimeConfig,
    output_dir: Path,
) -> dict[str, object]:
    with TemporaryDirectory(
        prefix="worktrace-isolated-date-",
        dir=output_dir,
    ) as temp_dir_name:
        temp_root = Path(temp_dir_name)
        isolated_config = replace(
            config,
            data_root=temp_root / "data",
            cache_root=temp_root / "cache",
            conversation_debug_root=temp_root / "debug",
            collected_merge_trace_enabled=False,
            collected_merge_trace_root=temp_root / "collected-trace-disabled",
            self_delivery_enabled=False,
        )
        started_at = perf_counter()
        result = run_daily_trace(target_date, isolated_config)
        output_path = Path(result.output_path).resolve() if result.output_path else None
        if output_path is not None and not output_path.is_relative_to(
            temp_root.resolve()
        ):
            raise RuntimeError(
                "Isolated date validation attempted to write outside its temporary directory."
            )
        output_exists = bool(output_path and output_path.is_file())
        return {
            "status": result.status,
            "event_count": result.event_count,
            "warning_count": result.warning_count,
            "self_delivery_status": result.self_delivery_status,
            "output_was_written_in_temporary_directory": output_exists,
            "temporary_directory_removed_after_validation": True,
            "wall_clock_ms": round((perf_counter() - started_at) * 1000, 3),
            "model_input_batch_target_tokens": config.model_input_batch_target_tokens,
            "stage_timing_summary": result.stage_timing_summary,
        }


def _validate_isolated_date_result(result: dict[str, object]) -> None:
    errors: list[str] = []
    if result.get("status") not in {"success", "success_with_warnings"}:
        errors.append("daily run did not complete")
    if result.get("self_delivery_status") != "disabled":
        errors.append("self delivery was not disabled")
    if result.get("output_was_written_in_temporary_directory") is not True:
        errors.append("temporary Markdown output was not written")
    if errors:
        raise RuntimeError(
            "Isolated date validation did not pass: " + "; ".join(errors)
        )


def _json_write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_existing_runs(
    output_dir: Path,
    *,
    dataset: dict[str, Any],
    thresholds: tuple[int, ...],
    primary_repeats: int,
    fallback_thresholds: tuple[int, ...],
    fallback_repeats: int,
    profile_id: str,
    primary_only: bool = False,
) -> list[dict[str, object]]:
    summary = load_json_object(output_dir / "benchmark-summary.json")
    if summary.get("schema_version") != 1:
        raise ValueError("Existing benchmark summary has an unsupported version.")
    if summary.get("dataset_version") != dataset.get("dataset_version"):
        raise ValueError("Existing benchmark dataset version does not match.")
    if summary.get("thresholds") != list(thresholds):
        raise ValueError("Existing benchmark thresholds do not match.")
    recorded_primary_repeats = summary.get(
        "primary_repeats",
        summary.get("repeats"),
    )
    if recorded_primary_repeats != primary_repeats:
        raise ValueError("Existing benchmark primary repeat count does not match.")
    if not primary_only:
        if summary.get("fallback_thresholds") != list(fallback_thresholds):
            raise ValueError("Existing benchmark fallback thresholds do not match.")
        if summary.get("fallback_repeats") != fallback_repeats:
            raise ValueError("Existing benchmark fallback repeat count does not match.")
    if summary.get("model_input_budget_profile_id", "") != profile_id:
        raise ValueError("Existing benchmark model profile does not match.")
    raw_runs = summary.get("runs")
    if not isinstance(raw_runs, list) or not raw_runs:
        raise ValueError("Existing benchmark summary does not contain runs.")

    runs: list[dict[str, object]] = []
    seen: set[tuple[str, int, int]] = set()
    for raw_run in raw_runs:
        if not isinstance(raw_run, dict):
            raise ValueError("Existing benchmark run is invalid.")
        route = raw_run.get("route")
        threshold = raw_run.get("threshold")
        repeat_index = raw_run.get("repeat_index")
        if (
            route not in {"primary", "fallback"}
            or not isinstance(threshold, int)
            or isinstance(threshold, bool)
            or not isinstance(repeat_index, int)
            or isinstance(repeat_index, bool)
        ):
            raise ValueError("Existing benchmark run identity is invalid.")
        identity = (route, threshold, repeat_index)
        if primary_only and route == "fallback":
            continue
        if identity in seen:
            raise ValueError("Existing benchmark contains duplicate runs.")
        seen.add(identity)
        result_payload = raw_run.get("result_payload")
        if not isinstance(result_payload, dict):
            raise ValueError("Existing benchmark run is missing generated results.")
        refreshed = dict(raw_run)
        refreshed["automatic_evaluation"] = evaluate_result(
            dataset,
            result_payload,
        )
        runs.append(refreshed)

    expected_primary = {
        ("primary", threshold, repeat_index)
        for threshold in thresholds
        for repeat_index in range(1, primary_repeats + 1)
    }
    actual_primary = {item for item in seen if item[0] == "primary"}
    if actual_primary != expected_primary:
        raise ValueError("Existing benchmark primary runs are incomplete.")
    if primary_only:
        return runs
    expected_fallback_baseline = {
        ("fallback", 7000, repeat_index)
        for repeat_index in range(1, fallback_repeats + 1)
    }
    actual_fallback = {item for item in seen if item[0] == "fallback"}
    if any(item[1] not in fallback_thresholds for item in actual_fallback):
        raise ValueError("Existing benchmark contains an unexpected fallback threshold.")
    if not expected_fallback_baseline.issubset(actual_fallback):
        raise ValueError("Existing benchmark fallback baseline is incomplete.")
    for threshold in fallback_thresholds:
        actual_repeats = {
            repeat_index
            for route, run_threshold, repeat_index in actual_fallback
            if run_threshold == threshold
        }
        if actual_repeats and actual_repeats != set(range(1, fallback_repeats + 1)):
            raise ValueError("Existing benchmark fallback runs are incomplete.")
    return runs


DEFAULT_DATASET_PATH = Path("tests/fixtures/event_generation_quality_cases.json")


def main(
    argv: list[str] | None = None,
    *,
    run_once: Callable[..., dict[str, object]] = run_threshold_once,
) -> int:
    parser = argparse.ArgumentParser(
        description="Run isolated model-input budget benchmarks with production contracts."
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_PATH)
    parser.add_argument("--thresholds", type=int, nargs="+", default=DEFAULT_THRESHOLDS)
    parser.add_argument(
        "--repeats",
        type=int,
        default=2,
        help="Number of repetitions for each primary threshold.",
    )
    parser.add_argument(
        "--fallback-thresholds",
        type=int,
        nargs="+",
        default=DEFAULT_FALLBACK_THRESHOLDS,
    )
    parser.add_argument("--fallback-repeats", type=int, default=1)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--manual-review", type=Path)
    parser.add_argument("--apply-selection", action="store_true")
    parser.add_argument("--isolate-date")
    reuse_group = parser.add_mutually_exclusive_group()
    reuse_group.add_argument("--reuse-existing", action="store_true")
    reuse_group.add_argument("--reuse-primary-existing", action="store_true")
    reuse_group.add_argument("--resume-existing", action="store_true")
    args = parser.parse_args(argv)
    if args.repeats <= 0:
        raise ValueError("--repeats must be positive.")
    if args.fallback_repeats <= 0:
        raise ValueError("--fallback-repeats must be positive.")
    thresholds = tuple(dict.fromkeys(args.thresholds))
    if any(value <= 0 for value in thresholds) or 7000 not in thresholds:
        raise ValueError("Thresholds must be positive and include the 7000 baseline.")
    fallback_thresholds = tuple(
        value
        for value in dict.fromkeys(args.fallback_thresholds)
        if value in thresholds
    )
    if (
        any(value <= 0 for value in args.fallback_thresholds)
        or 7000 not in fallback_thresholds
    ):
        raise ValueError(
            "Fallback thresholds must be positive, belong to the primary matrix, "
            "and include the 7000 baseline."
        )
    if args.apply_selection and args.manual_review is None:
        raise ValueError("--apply-selection requires a completed --manual-review file.")
    if args.apply_selection and not args.isolate_date:
        raise ValueError("--apply-selection requires a successful --isolate-date run.")
    if args.manual_review is not None and not args.reuse_existing:
        raise ValueError("--manual-review requires --reuse-existing benchmark results.")
    if args.isolate_date and not args.reuse_existing:
        raise ValueError("--isolate-date requires --reuse-existing benchmark results.")

    cwd = Path.cwd()
    dataset = load_json_object(args.dataset)
    config = load_runtime_config_overrides(RuntimeConfig(), cwd=cwd)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    profile_id = config.model_input_budget_selection.profile_id
    runs: list[dict[str, object]] = (
        _load_existing_runs(
            args.output_dir,
            dataset=dataset,
            thresholds=thresholds,
            primary_repeats=args.repeats,
            fallback_thresholds=fallback_thresholds,
            fallback_repeats=args.fallback_repeats,
            profile_id=profile_id,
            primary_only=args.reuse_primary_existing,
        )
        if args.reuse_existing or args.reuse_primary_existing or args.resume_existing
        else []
    )
    if args.resume_existing:
        runs = [
            run
            for run in runs
            if isinstance(run.get("mode_results"), list)
            and bool(run["mode_results"])
            and all(
                mode_result.get("status") == "success"
                and mode_result.get("python_validation_pass") is True
                for mode_result in run.get("mode_results", [])
            )
        ]

    if not args.reuse_existing and not args.reuse_primary_existing:
        existing_primary_runs = {
            (int(run["threshold"]), int(run["repeat_index"]))
            for run in runs
            if run["route"] == "primary"
        }
        for threshold in thresholds:
            for repeat_index in range(1, args.repeats + 1):
                if (threshold, repeat_index) in existing_primary_runs:
                    continue
                run = run_once(
                    dataset,
                    config=config,
                    route="primary",
                    threshold=threshold,
                    repeat_index=repeat_index,
                    cwd=cwd,
                )
                runs.append(run)
                _json_write(
                    args.output_dir
                    / f"primary-{threshold}-repeat-{repeat_index}.json",
                    run,
                )

    primary_baseline = [
        run for run in runs if run["route"] == "primary" and run["threshold"] == 7000
    ]
    primary_candidates: list[int] = []
    primary_gates: dict[str, object] = {}
    for threshold in thresholds:
        selected_runs = [
            run
            for run in runs
            if run["route"] == "primary" and run["threshold"] == threshold
        ]
        passed, errors = automatic_gate(selected_runs, primary_baseline)
        primary_gates[str(threshold)] = {"passed": passed, "errors": errors}
        if passed:
            primary_candidates.append(threshold)

    if (
        not args.reuse_existing
        and not args.reuse_primary_existing
        and 7000 not in primary_candidates
    ):
        primary_candidates = [7000]
    fallback_candidates = [
        threshold
        for threshold in fallback_thresholds
        if threshold in primary_candidates
    ]
    if not args.reuse_existing:
        existing_fallback_runs = {
            (int(run["threshold"]), int(run["repeat_index"]))
            for run in runs
            if run["route"] == "fallback"
        }
        for threshold in fallback_candidates:
            for repeat_index in range(1, args.fallback_repeats + 1):
                if (threshold, repeat_index) in existing_fallback_runs:
                    continue
                run = run_once(
                    dataset,
                    config=config,
                    route="fallback",
                    threshold=threshold,
                    repeat_index=repeat_index,
                    cwd=cwd,
                )
                runs.append(run)
                _json_write(
                    args.output_dir
                    / f"fallback-{threshold}-repeat-{repeat_index}.json",
                    run,
                )

    fallback_baseline = [
        run for run in runs if run["route"] == "fallback" and run["threshold"] == 7000
    ]
    fallback_gates: dict[str, object] = {}
    for threshold in fallback_candidates:
        selected_runs = [
            run
            for run in runs
            if run["route"] == "fallback" and run["threshold"] == threshold
        ]
        if len(selected_runs) != args.fallback_repeats:
            raise ValueError(
                f"Fallback benchmark runs are incomplete for threshold {threshold}."
            )
        passed, errors = automatic_gate(selected_runs, fallback_baseline)
        fallback_gates[str(threshold)] = {"passed": passed, "errors": errors}

    blind_items, review_mapping = _blind_review_artifacts(runs, dataset)
    _json_write(args.output_dir / "blind-review.json", {"items": blind_items})
    _json_write(args.output_dir / "blind-review-mapping.json", review_mapping)
    _json_write(
        args.output_dir / "manual-review-template.json",
        {
            review_id: {
                "fact_accurate": False,
                "completeness": 1,
                "readability": 1,
            }
            for review_id in review_mapping
        },
    )

    selection: dict[str, object] = {
        "selected_target_tokens": None,
        "reason": "manual_review_required",
    }
    if args.manual_review is not None:
        manual_scores = load_json_object(args.manual_review)
        automatic_passed_thresholds = {
            threshold
            for threshold in fallback_candidates
            if fallback_gates[str(threshold)]["passed"]
        }
        review_thresholds = {7000, *automatic_passed_thresholds}
        eligible_runs = [
            run for run in runs if run["threshold"] in review_thresholds
        ]
        eligible_mapping = {
            review_id: mapping
            for review_id, mapping in review_mapping.items()
            if mapping["threshold"] in review_thresholds
        }
        selection = select_threshold(
            eligible_runs,
            manual_scores,
            eligible_mapping,
            eligible_thresholds=automatic_passed_thresholds,
        )
        if args.apply_selection and selection["reason"] == "no_candidate_passed":
            raise ValueError(
                "No threshold passed both automatic checks and manual review."
            )

    isolated_validation = None
    if args.isolate_date:
        if selection["selected_target_tokens"] is None:
            raise ValueError("--isolate-date requires completed manual review selection.")
        if selection["reason"] == "no_candidate_passed":
            raise ValueError(
                "--isolate-date requires a threshold that passed manual review."
            )
        isolated_config = _config_for_threshold(
            config,
            int(selection["selected_target_tokens"]),
        )
        isolated_validation = run_isolated_date_validation(
            args.isolate_date,
            config=isolated_config,
            output_dir=args.output_dir,
        )
        _validate_isolated_date_result(isolated_validation)

    if args.apply_selection:
        _apply_profile_selection(
            config_path=cwd / config.model_input_budget_file_name,
            config=config,
            target_tokens=int(selection["selected_target_tokens"]),
            benchmark_dataset_version=str(dataset["dataset_version"]),
        )

    report = {
        "schema_version": 1,
        "dataset_version": str(dataset.get("dataset_version", "unknown")),
        "model_input_budget_profile_id": profile_id,
        "thresholds": list(thresholds),
        "primary_repeats": args.repeats,
        "repeats": args.repeats,
        "fallback_thresholds": list(fallback_thresholds),
        "fallback_repeats": args.fallback_repeats,
        "primary_gates": primary_gates,
        "fallback_gates": fallback_gates,
        "selection": selection,
        "isolated_date_validation": isolated_validation,
        "runs": runs,
    }
    _json_write(args.output_dir / "benchmark-summary.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
