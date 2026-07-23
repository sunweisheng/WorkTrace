from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.worktrace.analyzers.function_calls import task_function_call_spec
from src.worktrace.analyzers.output_schemas import collected_merge_output_schema
from src.worktrace.analyzers.prompts import build_collected_render_prompt
from src.worktrace.collected_merge import (
    CollectedMergeRunner,
    build_synthetic_collected_source_events,
    repair_collected_merge_result,
)
from src.worktrace.config import (
    DEFAULT_CONFIG,
    load_conversation_blacklist_overrides,
    load_runtime_config_overrides,
)
from src.worktrace.models import CollectedMergeResult, CollectedSourceEvent, WorkEvent
from src.worktrace.pipeline.retention_filter import (
    filter_retained_work_events,
    retention_rejection_reason_for_event,
)
from src.worktrace.utils.json_io import dump_json
from src.worktrace.utils.token_estimation import estimate_structured_input_tokens


SPECIFIC_TOKEN_RE = re.compile(
    r"(\d+(?:\.\d+)?%?|\d{4}年|\d+月|\d+日|v?\d+\.\d+(?:\.\d+)?|[A-Za-z0-9_./-]+\\.(?:md|pdf|xlsx?|docx?|sql))",
    re.IGNORECASE,
)


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_step_trace(path: Path, payload: dict[str, Any]) -> None:
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(dump_json(payload, pretty=True), encoding="utf-8")
    temporary_path.replace(path)


def _emit_step_status(
    *,
    step_index: int,
    status: str,
    prompt_chars: int,
    elapsed_ms: float | None = None,
    error_type: str = "",
) -> None:
    fields = [
        f'status="{status}"',
        f"step_index={step_index}",
        f"prompt_chars={prompt_chars}",
    ]
    if elapsed_ms is not None:
        fields.append(f"elapsed_ms={elapsed_ms:.1f}")
    if error_type:
        fields.append(f'error_type="{error_type}"')
    sys.stderr.write(f"{_now_utc_iso()} collected_merge.debug_step {' '.join(fields)}\n")
    sys.stderr.flush()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trace WorkTrace collected rolling merge specificity changes."
    )
    parser.add_argument("--date", required=True)
    parser.add_argument("--owner", default="孙维晟")
    parser.add_argument(
        "--output-dir",
        default="data/debug/collected_merge",
        help="Directory for diagnostic JSON and Markdown output.",
    )
    args = parser.parse_args()

    cwd = Path.cwd()
    config = load_conversation_blacklist_overrides(
        load_runtime_config_overrides(DEFAULT_CONFIG, cwd=cwd),
        cwd=cwd,
    )
    trace_dir = Path(args.output_dir) / args.date
    trace_dir.mkdir(parents=True, exist_ok=True)

    runner = TracingCollectedMergeRunner(
        config=config,
        trace_dir=trace_dir,
    )
    input_dir = runner.build_input_dir(args.date)
    output_path = input_dir / f"{args.date}-{args.owner}-merged.md"
    (
        source_events,
        source_file_count,
        skipped_file_count,
        partial_file_count,
        read_warnings,
        _source_audit,
    ) = (
        runner._read_source_events(
            args.date,
            input_dir,
            output_path=output_path,
            ignored_subdirectories=set(),
        )
    )
    source_events, source_retention_warnings = runner._filter_retained_source_events(
        source_events,
    )
    source_events, owner_warnings = runner._mark_merge_owner_sources(
        source_events,
        merge_owner_person=args.owner,
        input_dir=input_dir,
    )
    merged_events, merge_warnings = runner._merge_source_events(
        args.date,
        source_events,
        merge_owner_person=args.owner,
    )

    summary = {
        "target_date": args.date,
        "input_dir": str(input_dir.resolve()),
        "source_file_count": source_file_count,
        "skipped_file_count": skipped_file_count,
        "partial_file_count": partial_file_count,
        "source_event_count_after_source_filter": len(source_events),
        "final_event_count": len(merged_events),
        "final_source_id_count": len(_source_ids_from_events(merged_events)),
        "read_warnings": read_warnings,
        "source_retention_warnings": source_retention_warnings,
        "owner_warnings": owner_warnings,
        "merge_warnings": merge_warnings,
        "steps": runner.step_summaries,
        "batch_decisions": runner._collected_merge_batch_decisions,
    }
    (trace_dir / "summary.json").write_text(dump_json(summary, pretty=True), encoding="utf-8")
    (trace_dir / "summary.md").write_text(_render_summary_markdown(summary), encoding="utf-8")
    print((trace_dir / "summary.md").resolve())


class TracingCollectedMergeRunner(CollectedMergeRunner):
    def __init__(self, *args: Any, trace_dir: Path, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.trace_dir = trace_dir
        self.step_summaries: list[dict[str, Any]] = []

    def _merge_collected_event_batch(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        *,
        deterministic_groups: list[list[str]] | None = None,
        rolling_step_index: int = 1,
    ) -> tuple[list[WorkEvent], list[str]]:
        deterministic_groups = (
            deterministic_groups
            if deterministic_groups is not None
            else self._build_deterministic_groups(source_events)[0]
        )
        step_index = len(self.step_summaries) + 1
        prompt = build_collected_render_prompt(
            target_date,
            source_events,
            deterministic_groups,
            config=self.config,
        )
        function_spec = task_function_call_spec(
            "collected_event_merge",
            collected_merge_output_schema(),
            draft_ids=[item.draft_id for item in source_events],
            exact_array_lengths={"groups": len(deterministic_groups)},
        )
        input_estimates = estimate_structured_input_tokens(
            prompt,
            function_spec=function_spec,
            append_no_think=True,
        )
        input_metrics = _metrics_for_source_events(source_events)
        step_path = self.trace_dir / f"step-{step_index:03d}.json"
        started_at_utc = _now_utc_iso()
        started_at = perf_counter()
        step_payload = {
            "step_index": step_index,
            "status": "running",
            "started_at_utc": started_at_utc,
            "completed_at_utc": None,
            "elapsed_ms": None,
            "prompt_chars": len(prompt),
            **input_estimates,
            "input_target_tokens": self.config.model_input_batch_target_tokens,
            "function_name": function_spec.name,
            "function_definition": function_spec.tool(),
            "input": input_metrics,
        }
        _write_step_trace(step_path, step_payload)
        _emit_step_status(
            step_index=step_index,
            status="running",
            prompt_chars=len(prompt),
        )

        try:
            raw_result, retry_warnings = self._invoke_collected_merge_with_retry(
                target_date,
                source_events,
                deterministic_groups,
                rolling_step_index=rolling_step_index,
            )
            repaired_result, repair_warnings = repair_collected_merge_result(
                raw_result,
                source_events,
                deterministic_groups,
            )
            repaired_result, metadata_warnings = self._fill_collected_merge_group_metadata(
                source_events,
                repaired_result,
            )
            materialized_events = self._materialize_events(
                target_date,
                source_events,
                repaired_result,
            )
            sensitive_events, sensitive_warnings = self._filter_sensitive_events(
                target_date,
                materialized_events,
            )
            retained_events, retention_warnings = filter_retained_work_events(
                sensitive_events,
            )

            dropped_by_retention = [
                {
                    "title": event.title,
                    "source_event_ids": list(event.source_event_ids),
                    "retention_reason": event.retention_reason,
                    "retention_detail": event.retention_detail,
                    "rejection_reason": retention_rejection_reason_for_event(event),
                }
                for event in sensitive_events
                if retention_rejection_reason_for_event(event)
            ]
            elapsed_ms = (perf_counter() - started_at) * 1000
            step_payload.update(
                {
                    "status": "success",
                    "completed_at_utc": _now_utc_iso(),
                    "elapsed_ms": round(elapsed_ms, 3),
                    "raw_group_count": len(raw_result.groups),
                    "raw_group_metrics": _metrics_for_groups(raw_result),
                    "repaired_group_count": len(repaired_result.groups),
                    "materialized_metrics": _metrics_for_work_events(materialized_events),
                    "after_sensitive_metrics": _metrics_for_work_events(sensitive_events),
                    "retained_metrics": _metrics_for_work_events(retained_events),
                    "repair_warnings": repair_warnings,
                    "retry_warnings": retry_warnings,
                    "metadata_warnings": metadata_warnings,
                    "sensitive_warnings": sensitive_warnings,
                    "retention_warnings": retention_warnings,
                    "dropped_by_retention": dropped_by_retention,
                    "raw_result": raw_result.to_dict(),
                    "repaired_result": repaired_result.to_dict(),
                    "retained_events": [event.to_dict() for event in retained_events],
                }
            )
            _write_step_trace(step_path, step_payload)
            _emit_step_status(
                step_index=step_index,
                status="success",
                prompt_chars=len(prompt),
                elapsed_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = (perf_counter() - started_at) * 1000
            step_payload.update(
                {
                    "status": "failed",
                    "completed_at_utc": _now_utc_iso(),
                    "elapsed_ms": round(elapsed_ms, 3),
                    "error": {
                        "type": type(exc).__name__,
                        "summary": str(exc),
                    },
                }
            )
            _write_step_trace(step_path, step_payload)
            _emit_step_status(
                step_index=step_index,
                status="failed",
                prompt_chars=len(prompt),
                elapsed_ms=elapsed_ms,
                error_type=type(exc).__name__,
            )
            raise

        self.step_summaries.append(
            {
                key: value
                for key, value in step_payload.items()
                if key not in {"raw_result", "repaired_result", "retained_events"}
            }
        )
        return retained_events, [
            *repair_warnings,
            *sensitive_warnings,
            *retention_warnings,
        ]


def _source_ids_from_source_events(events: list[CollectedSourceEvent]) -> set[str]:
    values: set[str] = set()
    for item in events:
        values.update(item.event.source_event_ids or [item.event.event_id])
    return {value for value in values if value}


def _source_ids_from_events(events: list[WorkEvent]) -> set[str]:
    values: set[str] = set()
    for event in events:
        values.update(event.source_event_ids or [event.event_id])
    return {value for value in values if value}


def _metrics_for_source_events(events: list[CollectedSourceEvent]) -> dict[str, Any]:
    return {
        "event_count": len(events),
        "synthetic_event_count": sum(
            1 for item in events if item.source_file.startswith("__rolling_collected_merge_step_")
        ),
        "source_file_count": len({item.source_file for item in events}),
        "source_id_count": len(_source_ids_from_source_events(events)),
        "text_metrics": _text_metrics(
            [
                {
                    "title": item.event.title,
                    "content": item.event.content,
                    "object_hint": item.event.object_hint,
                    "retention_detail": item.event.retention_detail,
                }
                for item in events
            ]
        ),
    }


def _metrics_for_groups(result: CollectedMergeResult) -> dict[str, Any]:
    return {
        "group_count": len(result.groups),
        "draft_ref_count": sum(len(group.draft_ids) for group in result.groups),
        "text_metrics": _text_metrics(
            [
                {
                    "title": group.title,
                    "content": group.content,
                    "object_hint": group.object_hint,
                    "retention_detail": group.retention_detail,
                }
                for group in result.groups
            ]
        ),
    }


def _metrics_for_work_events(events: list[WorkEvent]) -> dict[str, Any]:
    rejection_reasons = Counter(
        reason
        for event in events
        for reason in [retention_rejection_reason_for_event(event)]
        if reason
    )
    return {
        "event_count": len(events),
        "source_id_count": len(_source_ids_from_events(events)),
        "retention_rejection_reasons_if_filtered_now": dict(rejection_reasons),
        "text_metrics": _text_metrics(
            [
                {
                    "title": event.title,
                    "content": event.content,
                    "object_hint": event.object_hint,
                    "retention_detail": event.retention_detail,
                }
                for event in events
            ]
        ),
    }


def _text_metrics(rows: list[dict[str, str]]) -> dict[str, Any]:
    fields = ("title", "content", "object_hint", "retention_detail")
    result: dict[str, Any] = {}
    for field in fields:
        lengths = [len((row.get(field) or "").strip()) for row in rows]
        result[f"{field}_avg_len"] = _avg(lengths)
        result[f"{field}_median_len"] = _median(lengths)
        result[f"{field}_min_len"] = min(lengths) if lengths else 0
        result[f"{field}_max_len"] = max(lengths) if lengths else 0
    combined = "\n".join(
        "\n".join([row.get("title", ""), row.get("content", ""), row.get("retention_detail", "")])
        for row in rows
    )
    result["specific_token_count"] = len(SPECIFIC_TOKEN_RE.findall(combined))
    result["specific_token_per_event"] = (
        round(result["specific_token_count"] / len(rows), 3) if rows else 0
    )
    return result


def _avg(values: list[int]) -> float:
    if not values:
        return 0
    return round(sum(values) / len(values), 2)


def _median(values: list[int]) -> float:
    if not values:
        return 0
    return round(float(median(values)), 2)


def _render_summary_markdown(summary: dict[str, Any]) -> str:
    lines = [
        f"# Rolling Merge Specificity Diagnosis · {summary['target_date']}",
        "",
        "## Summary",
        "",
        f"- Source events after source filter: {summary['source_event_count_after_source_filter']}",
        f"- Final events: {summary['final_event_count']}",
        f"- Final source IDs: {summary['final_source_id_count']}",
        "",
        "## Step Metrics",
        "",
        "| Step | Input estimate | Online estimate | Codex estimate | Target | Prompt chars | Input events | Synthetic inputs | Input source IDs | Raw groups | Retained events | Retained source IDs | Dropped by retention | missing_or_generic_retention_detail | Detail median in | Detail median raw | Detail median kept | Specific tokens/event raw | Specific tokens/event kept |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for step in summary["steps"]:
        retention_counts = Counter(
            item["rejection_reason"]
            for item in step["dropped_by_retention"]
            if item.get("rejection_reason")
        )
        input_text = step["input"]["text_metrics"]
        raw_text = step["raw_group_metrics"]["text_metrics"]
        kept_text = step["retained_metrics"]["text_metrics"]
        lines.append(
            "| {step} | {input_estimate} | {online_estimate} | {codex_estimate} | {target} | {prompt} | {input_events} | {synthetic} | {input_ids} | {raw_groups} | {kept_events} | {kept_ids} | {dropped} | {missing_detail} | {detail_in} | {detail_raw} | {detail_kept} | {specific_raw} | {specific_kept} |".format(
                step=step["step_index"],
                input_estimate=step["input_estimated_tokens"],
                online_estimate=step["online_input_estimated_tokens"],
                codex_estimate=step["codex_input_estimated_tokens"],
                target=step["input_target_tokens"],
                prompt=step["prompt_chars"],
                input_events=step["input"]["event_count"],
                synthetic=step["input"]["synthetic_event_count"],
                input_ids=step["input"]["source_id_count"],
                raw_groups=step["raw_group_count"],
                kept_events=step["retained_metrics"]["event_count"],
                kept_ids=step["retained_metrics"]["source_id_count"],
                dropped=len(step["dropped_by_retention"]),
                missing_detail=retention_counts.get("missing_or_generic_retention_detail", 0),
                detail_in=input_text["retention_detail_median_len"],
                detail_raw=raw_text["retention_detail_median_len"],
                detail_kept=kept_text["retention_detail_median_len"],
                specific_raw=raw_text["specific_token_per_event"],
                specific_kept=kept_text["specific_token_per_event"],
            )
        )
    lines.extend(
        [
            "",
            "## Candidate Batch Decisions",
            "",
            "| Decision | Level | Action | Estimated input tokens | Target | Added draft IDs |",
            "|---:|---|---|---:|---:|---|",
        ]
    )
    for decision in summary.get("batch_decisions", []):
        lines.append(
            f"| {decision['decision_index']} | {decision['level']} | "
            f"{decision['action']} | {decision['input_estimated_tokens']} | "
            f"{decision['input_target_tokens']} | "
            f"{', '.join(decision['added_draft_ids'])} |"
        )
    lines.extend(["", "## Retention Drops", ""])
    for step in summary["steps"]:
        lines.append(f"### Step {step['step_index']}")
        dropped = step["dropped_by_retention"]
        if not dropped:
            lines.append("")
            lines.append("- None")
            lines.append("")
            continue
        lines.append("")
        for item in dropped:
            lines.append(
                f"- {item['title']} | {item['rejection_reason']} | {item['retention_detail']}"
            )
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
