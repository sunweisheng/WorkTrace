from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
from time import perf_counter

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.worktrace.config import (
    DEFAULT_CONFIG,
    load_conversation_blacklist_overrides,
    load_runtime_config_overrides,
)
from src.worktrace.factories import AnalyzerFactory, RuntimeDependencies
from src.worktrace.llm_usage import LLMUsageRecorder
from src.worktrace.logging_utils import configure_logging
from src.worktrace.models import CrossConversationGroup, SourceBackedEventDraft
from src.worktrace.pipeline.day_event_grouping import DayGroupReviewComponent
from src.worktrace.runner import DailyTraceRunner
from scripts.replay_day_with_trace import _collect_day_grouping_artifact_summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--component-id", action="append", default=[])
    parser.add_argument("--trace-root", default=None)
    return parser.parse_args()


def _load_object(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return payload


def _failed_component_ids(
    components: list[dict[str, object]],
    attempts: list[dict[str, object]],
) -> list[str]:
    latest_status: dict[str, str] = {}
    for attempt in attempts:
        component_id = str(attempt.get("component_id", ""))
        if component_id:
            latest_status[component_id] = str(attempt.get("status", ""))
    return [
        str(component.get("component_id", ""))
        for component in components
        if latest_status.get(str(component.get("component_id", ""))) != "success"
    ]


def _latest_validation_feedback(attempts: list[dict[str, object]]) -> str:
    for attempt in reversed(attempts):
        if str(attempt.get("failure_kind", "")) != "validation":
            continue
        feedback = str(attempt.get("validation_error", "")).strip()
        if feedback:
            return feedback
    return ""


def main() -> int:
    args = _parse_args()
    repo_root = Path.cwd()
    trace_root = (
        Path(args.trace_root)
        if args.trace_root
        else repo_root / "data" / "replay-trace" / args.date
    )
    merge_root = (
        trace_root
        / "conversation_debug"
        / args.date
        / "_merge_day_candidates"
    )
    input_payload = _load_object(merge_root / "input.json")
    review_payload = _load_object(merge_root / "day_group_review.json")
    raw_candidates = input_payload.get("candidates", [])
    raw_components = review_payload.get("components", [])
    raw_attempts = review_payload.get("attempts", [])
    candidates = [
        SourceBackedEventDraft.from_dict(item)
        for item in raw_candidates
        if isinstance(item, dict)
    ]
    components = [item for item in raw_components if isinstance(item, dict)]
    attempts = [item for item in raw_attempts if isinstance(item, dict)]
    selected_ids = list(dict.fromkeys(args.component_id)) or _failed_component_ids(
        components,
        attempts,
    )
    if not selected_ids:
        print(json.dumps({"status": "no_failed_components"}, ensure_ascii=False))
        return 0

    config = load_runtime_config_overrides(DEFAULT_CONFIG, cwd=repo_root)
    config = load_conversation_blacklist_overrides(config, cwd=repo_root)
    config = replace(config, analyzer_backend="online", codex_stdin_mode=False)
    usage_recorder = LLMUsageRecorder()
    analyzer = AnalyzerFactory.create_default(
        config,
        usage_recorder=usage_recorder,
    )
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=object(),
            content_resolver=object(),
            analyzer=analyzer,
            delivery_channel=object(),
            event_store=object(),
            llm_usage_recorder=usage_recorder,
        ),
    )
    candidate_by_id = {item.draft_id: item for item in candidates}
    component_by_id = {
        str(item.get("component_id", "")): item for item in components
    }
    attempts_by_component: dict[str, list[dict[str, object]]] = {}
    for attempt in attempts:
        attempts_by_component.setdefault(
            str(attempt.get("component_id", "")),
            [],
        ).append(attempt)

    configure_logging()
    replay_runs: list[dict[str, object]] = []
    for component_id in selected_ids:
        component_meta = component_by_id.get(component_id)
        source_attempts = attempts_by_component.get(component_id, [])
        if component_meta is None or not source_attempts:
            raise ValueError(f"Unknown or unrecorded component: {component_id}")
        source_input = source_attempts[-1].get("input", {})
        if not isinstance(source_input, dict):
            raise ValueError(f"Missing review input for component: {component_id}")
        raw_groups = source_input.get("groups", [])
        group_items = [item for item in raw_groups if isinstance(item, dict)]
        groups = [CrossConversationGroup.from_dict(item) for item in group_items]
        raw_candidate_ids = source_input.get("candidate_draft_ids", [])
        candidate_ids = [
            str(item) for item in raw_candidate_ids if isinstance(item, str)
        ]
        component_candidates = [
            candidate_by_id[draft_id]
            for draft_id in candidate_ids
            if draft_id in candidate_by_id
        ]
        initial_feedback = _latest_validation_feedback(source_attempts)
        component = DayGroupReviewComponent(
            component_id=component_id,
            groups=groups,
            candidates=component_candidates,
            relation_reasons=list(component_meta.get("relation_reasons", [])),
            relation_sources=[
                str(item) for item in component_meta.get("relation_sources", [])
            ],
        )
        started_at = perf_counter()
        (
            _returned_component_id,
            result,
            replay_attempts,
            warnings,
            retry_count,
            codex_fallback_count,
        ) = runner._review_day_group_component(
            target_date=args.date,
            component=component,
            initial_validation_feedback=initial_feedback,
        )
        replay_runs.append(
            {
                "component_id": component_id,
                "status": "success" if result is not None else "abandoned",
                "duration_ms": round((perf_counter() - started_at) * 1000, 3),
                "initial_validation_feedback": initial_feedback,
                "source_attempt_count": len(source_attempts),
                "retry_count": retry_count,
                "codex_fallback_count": codex_fallback_count,
                "attempts": replay_attempts,
                "warnings": warnings,
                "result": result.to_dict() if result is not None else None,
            }
        )

    artifact = {
        "target_date": args.date,
        "source_trace_root": str(trace_root.resolve()),
        "selected_component_ids": selected_ids,
        "runs": replay_runs,
        "usage_attempts": usage_recorder.records(),
    }
    output_path = merge_root / "day_group_review_replay.json"
    output_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary_path = trace_root / "summary.json"
    if summary_path.exists():
        summary = _load_object(summary_path)
        summary["day_grouping_artifact_summary"] = (
            _collect_day_grouping_artifact_summary(
                trace_root / "conversation_debug",
                args.date,
            )
        )
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "status": (
                    "success"
                    if all(item["status"] == "success" for item in replay_runs)
                    else "completed_with_abandoned_components"
                ),
                "output_path": str(output_path.resolve()),
                "runs": [
                    {
                        key: item[key]
                        for key in (
                            "component_id",
                            "status",
                            "duration_ms",
                            "retry_count",
                            "codex_fallback_count",
                        )
                    }
                    for item in replay_runs
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
