from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.worktrace.analyzers.output_schemas import collected_grouping_output_schema
from src.worktrace.analyzers.prompts import build_collected_review_prompt
from src.worktrace.analyzers.protocol import parse_collected_grouping_payload
from src.worktrace.collected_merge import (
    _collected_review_result_basis_error,
    collected_grouping_partition_error,
    repair_collected_grouping_result,
)
from src.worktrace.config import (
    DEFAULT_CONFIG,
    RuntimeConfig,
    load_conversation_blacklist_overrides,
    load_runtime_config_overrides,
)
from src.worktrace.models import (
    CollectedGroupingGroup,
    CollectedGroupingResult,
    CollectedSourceEvent,
    WorkEvent,
)

DEFAULT_INVENTORY = Path(
    "data/debug/online_llm_failure_inventory/20260723/summary.json"
)
DEFAULT_CASE_IDS = ("M07", "M08", "M09", "M10", "M11")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "离线重建并校验多人汇总高风险复核失败素材，不调用在线模型。"
        )
    )
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument(
        "--ids",
        default=",".join(DEFAULT_CASE_IDS),
        help="逗号分隔的素材编号，默认 M07-M11。",
    )
    parser.add_argument(
        "--result-dir",
        type=Path,
        help="可选；优先读取此目录下的 M07.json 等新实验结果。",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="可选；写入最新 prompt、schema、summary.json 和 summary.md。",
    )
    parser.add_argument("--json", action="store_true", help="在终端输出 JSON。")
    return parser.parse_args()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def resolve_repo_path(path: Path) -> Path:
    return path if path.is_absolute() else REPO_ROOT / path


def load_runtime_config() -> RuntimeConfig:
    config = load_runtime_config_overrides(DEFAULT_CONFIG, cwd=REPO_ROOT)
    return load_conversation_blacklist_overrides(config, cwd=REPO_ROOT)


def load_inventory_cases(
    inventory_path: Path,
    case_ids: tuple[str, ...],
) -> list[dict[str, Any]]:
    payload = read_json(inventory_path)
    items = {
        str(item.get("id", "")): item
        for item in payload.get("items", [])
        if isinstance(item, dict)
    }
    missing = [case_id for case_id in case_ids if case_id not in items]
    if missing:
        raise ValueError(f"Inventory does not contain cases: {', '.join(missing)}")
    return [items[case_id] for case_id in case_ids]


def trace_path_from_prompt(prompt_path: Path) -> Path:
    suffix = "-prompt.txt"
    if not prompt_path.name.endswith(suffix):
        raise ValueError(f"Unexpected collected review prompt path: {prompt_path}")
    return prompt_path.with_name(prompt_path.name.removesuffix(suffix) + ".json")


def source_event_from_trace(payload: dict[str, Any]) -> CollectedSourceEvent:
    event_payload = payload.get("event")
    if not isinstance(event_payload, dict):
        raise ValueError("Collected review trace input event is missing `event`.")
    return CollectedSourceEvent(
        draft_id=str(payload.get("draft_id", "")),
        person_name=str(payload.get("person_name", "")),
        source_file=str(payload.get("source_file", "")),
        event=WorkEvent.from_dict(event_payload),
        source_report_owner=str(payload.get("source_report_owner", "")),
        is_merge_owner_source=bool(payload.get("is_merge_owner_source", False)),
        candidate_summary_source=str(payload.get("candidate_summary_source", "")),
        prompt_original_content_chars=payload.get("prompt_original_content_chars"),
    )


def load_result_payload(trace: dict[str, Any], override_path: Path | None) -> Any:
    payload = read_json(override_path) if override_path is not None else trace.get("raw_result")
    if isinstance(payload, dict) and isinstance(payload.get("raw_result"), dict):
        return payload["raw_result"]
    return payload


def effective_split_reason(result: CollectedGroupingResult) -> str:
    if result.split_reason.strip():
        return result.split_reason.strip()
    return next(
        (
            group.split_reason.strip()
            for group in result.groups
            if group.split_reason.strip()
        ),
        "",
    )


def evaluate_review_result(
    *,
    result: CollectedGroupingResult,
    source_events: list[CollectedSourceEvent],
    candidate_group: CollectedGroupingGroup,
    review_reasons: list[str],
    config: RuntimeConfig,
) -> dict[str, Any]:
    partition_error = collected_grouping_partition_error(
        result,
        candidate_group.draft_ids,
    )
    relation_error = _collected_review_result_basis_error(
        result,
        source_events,
        reasons=review_reasons,
        config=config,
    )
    repaired, repair_warnings = repair_collected_grouping_result(
        result,
        source_events,
        [],
    )
    split_reason = effective_split_reason(repaired)
    split_reason_error = bool(
        len(candidate_group.draft_ids) > 1
        and len(repaired.groups) > 1
        and not split_reason
    )
    errors = [
        error
        for error in (
            partition_error,
            relation_error,
            "missing_overall_split_reason" if split_reason_error else "",
        )
        if error
    ]
    return {
        "valid": not errors,
        "errors": errors,
        "group_count": len(repaired.groups),
        "split_reason": split_reason,
        "repair_warnings": repair_warnings,
        "groups": [
            {
                "group_id": group.group_id,
                "draft_ids": list(group.draft_ids),
                "group_reason": list(group.group_reason),
            }
            for group in repaired.groups
        ],
    }


def replay_case(
    item: dict[str, Any],
    *,
    config: RuntimeConfig,
    result_dir: Path | None = None,
) -> tuple[dict[str, Any], str]:
    case_id = str(item["id"])
    original_prompt_path = resolve_repo_path(Path(str(item["prompt_path"])))
    trace_path = trace_path_from_prompt(original_prompt_path)
    trace = read_json(trace_path)
    source_events = [
        source_event_from_trace(payload)
        for payload in trace.get("input_events", [])
        if isinstance(payload, dict)
    ]
    candidate_payload = trace.get("candidate_group")
    if not isinstance(candidate_payload, dict):
        raise ValueError(f"{case_id} trace has no candidate_group.")
    candidate_group = CollectedGroupingGroup.from_dict(candidate_payload)
    review_reasons = [str(value) for value in trace.get("review_reasons", [])]
    override_path = result_dir / f"{case_id}.json" if result_dir else None
    if override_path is not None and not override_path.exists():
        raise FileNotFoundError(override_path)
    result = parse_collected_grouping_payload(
        load_result_payload(trace, override_path)
    )
    evaluation = evaluate_review_result(
        result=result,
        source_events=source_events,
        candidate_group=candidate_group,
        review_reasons=review_reasons,
        config=config,
    )
    current_prompt = build_collected_review_prompt(
        str(item.get("target_date", "")),
        source_events,
        candidate_group,
        config=config,
        review_reasons=review_reasons,
    )
    evaluation.update(
        {
            "id": case_id,
            "failure_types": list(item.get("failure_types", [])),
            "source_trace": str(trace_path.relative_to(REPO_ROOT)),
            "result_source": (
                str(override_path.relative_to(REPO_ROOT))
                if override_path is not None and override_path.is_relative_to(REPO_ROOT)
                else str(override_path)
                if override_path is not None
                else "recorded_raw_result"
            ),
        }
    )
    return evaluation, current_prompt


def build_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "case_count": len(results),
        "valid_count": sum(bool(item["valid"]) for item in results),
        "invalid_count": sum(not bool(item["valid"]) for item in results),
        "results": results,
    }


def build_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# 多人汇总高风险复核离线回放",
        "",
        f"- 素材数：{summary['case_count']}",
        f"- 当前规则通过：{summary['valid_count']}",
        f"- 当前规则不通过：{summary['invalid_count']}",
        "",
        "| 编号 | 当前规则 | 分组数 | 错误 |",
        "|---|---|---:|---|",
    ]
    for item in summary["results"]:
        errors = "；".join(item["errors"]) or "-"
        lines.append(
            f"| {item['id']} | {'通过' if item['valid'] else '不通过'} | "
            f"{item['group_count']} | {errors} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    inventory_path = resolve_repo_path(args.inventory)
    result_dir = resolve_repo_path(args.result_dir) if args.result_dir else None
    output_dir = resolve_repo_path(args.output_dir) if args.output_dir else None
    case_ids = tuple(
        value.strip() for value in str(args.ids).split(",") if value.strip()
    )
    config = load_runtime_config()
    items = load_inventory_cases(inventory_path, case_ids)
    results: list[dict[str, Any]] = []
    prompts: dict[str, str] = {}
    for item in items:
        evaluation, prompt = replay_case(
            item,
            config=config,
            result_dir=result_dir,
        )
        results.append(evaluation)
        prompts[str(item["id"])] = prompt
    summary = build_summary(results)

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        write_json(output_dir / "schema.json", collected_grouping_output_schema(config))
        write_json(output_dir / "summary.json", summary)
        (output_dir / "summary.md").write_text(
            build_markdown(summary),
            encoding="utf-8",
        )
        for case_id, prompt in prompts.items():
            (output_dir / f"{case_id}-prompt.txt").write_text(
                prompt,
                encoding="utf-8",
            )

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for item in results:
            errors = "; ".join(item["errors"]) or "none"
            print(
                f"{item['id']} valid={str(item['valid']).lower()} "
                f"groups={item['group_count']} errors={errors}"
            )
        print(
            f"SUMMARY cases={summary['case_count']} valid={summary['valid_count']} "
            f"invalid={summary['invalid_count']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
