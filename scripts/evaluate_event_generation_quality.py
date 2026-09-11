from __future__ import annotations

import argparse
from itertools import combinations
import json
from pathlib import Path
from typing import Any


REQUIRED_METADATA_FIELDS = (
    "variant",
    "model",
    "service",
    "reasoning_effort",
    "stream_enabled",
)
REQUIRED_METRIC_FIELDS = (
    "input_estimated_tokens",
    "model_call_count",
    "retry_count",
    "elapsed_ms",
)


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON file: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return payload


def evaluate_result(
    dataset: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    cases = _read_dataset_cases(dataset)
    metadata = _read_metadata(result)
    result_cases = result.get("cases")
    if not isinstance(result_cases, list):
        raise ValueError("Result field `cases` must be a list.")
    result_by_id: dict[str, dict[str, Any]] = {}
    for item in result_cases:
        if not isinstance(item, dict) or not isinstance(item.get("case_id"), str):
            raise ValueError("Each result case must contain a string case_id.")
        case_id = item["case_id"].strip()
        if not case_id or case_id in result_by_id:
            raise ValueError("Result case_id values must be non-empty and unique.")
        result_by_id[case_id] = item
    if set(result_by_id) != set(cases):
        raise ValueError("Result case ids must exactly match the evaluation dataset.")

    totals = {
        "case_count": len(cases),
        "event_count": 0,
        "expected_source_count": 0,
        "assigned_source_count": 0,
        "source_covered_count": 0,
        "missing_source_assignment_count": 0,
        "duplicate_source_assignment_count": 0,
        "unknown_source_assignment_count": 0,
        "missing_fact_source_count": 0,
        "invalid_fact_source_reference_count": 0,
        "incorrect_split_pair_count": 0,
        "incorrect_merge_pair_count": 0,
        "forbidden_style_hit_count": 0,
        "input_estimated_tokens_total": 0,
        "input_estimated_tokens_max": 0,
        "model_call_count": 0,
        "retry_count": 0,
        "elapsed_ms": 0,
    }
    case_summaries: list[dict[str, Any]] = []
    for case_id, case in cases.items():
        summary = _evaluate_case(case, result_by_id[case_id])
        case_summaries.append(summary)
        totals["event_count"] += summary["event_count"]
        totals["expected_source_count"] += summary["expected_source_count"]
        totals["assigned_source_count"] += summary["assigned_source_count"]
        totals["source_covered_count"] += summary["source_covered_count"]
        for key in (
            "missing_source_assignment_count",
            "duplicate_source_assignment_count",
            "unknown_source_assignment_count",
            "missing_fact_source_count",
            "invalid_fact_source_reference_count",
            "incorrect_split_pair_count",
            "incorrect_merge_pair_count",
            "forbidden_style_hit_count",
            "input_estimated_tokens",
            "model_call_count",
            "retry_count",
            "elapsed_ms",
        ):
            total_key = (
                "input_estimated_tokens_total"
                if key == "input_estimated_tokens"
                else key
            )
            totals[total_key] += summary[key]
        totals["input_estimated_tokens_max"] = max(
            totals["input_estimated_tokens_max"],
            summary["input_estimated_tokens"],
        )
    expected_source_count = totals["expected_source_count"]
    totals["source_coverage_rate"] = (
        totals["source_covered_count"] / expected_source_count
        if expected_source_count
        else 1.0
    )
    totals["fixed_format_pass"] = all(
        totals[key] == 0
        for key in (
            "missing_source_assignment_count",
            "duplicate_source_assignment_count",
            "unknown_source_assignment_count",
            "missing_fact_source_count",
            "invalid_fact_source_reference_count",
        )
    )
    totals["automatic_quality_issue_count"] = sum(
        totals[key]
        for key in (
            "missing_source_assignment_count",
            "duplicate_source_assignment_count",
            "unknown_source_assignment_count",
            "missing_fact_source_count",
            "invalid_fact_source_reference_count",
            "incorrect_split_pair_count",
            "incorrect_merge_pair_count",
            "forbidden_style_hit_count",
        )
    )
    return {
        "metadata": metadata,
        "summary": totals,
        "cases": case_summaries,
    }


def compare_results(
    dataset: dict[str, Any],
    baseline: dict[str, Any],
    optimized: dict[str, Any],
) -> dict[str, Any]:
    baseline_report = evaluate_result(dataset, baseline)
    optimized_report = evaluate_result(dataset, optimized)
    _validate_comparable_metadata(
        baseline_report["metadata"],
        optimized_report["metadata"],
    )
    comparable_metrics = (
        "event_count",
        "source_coverage_rate",
        "incorrect_split_pair_count",
        "incorrect_merge_pair_count",
        "forbidden_style_hit_count",
        "automatic_quality_issue_count",
        "input_estimated_tokens_total",
        "input_estimated_tokens_max",
        "model_call_count",
        "retry_count",
        "elapsed_ms",
    )
    delta = {
        key: optimized_report["summary"][key] - baseline_report["summary"][key]
        for key in comparable_metrics
    }
    return {
        "schema_version": 1,
        "baseline": baseline_report,
        "optimized": optimized_report,
        "delta_optimized_minus_baseline": delta,
        "manual_review_required": [
            "事实准确性",
            "事项完整性",
            "可读性",
        ],
    }


def _read_dataset_cases(dataset: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if type(dataset.get("schema_version")) is not int or dataset.get(
        "schema_version"
    ) != 1:
        raise ValueError("Dataset schema_version must be 1.")
    raw_cases = dataset.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("Dataset field `cases` must be a non-empty list.")
    cases: dict[str, dict[str, Any]] = {}
    for case in raw_cases:
        if not isinstance(case, dict) or not isinstance(case.get("case_id"), str):
            raise ValueError("Each dataset case must contain a string case_id.")
        case_id = case["case_id"].strip()
        if not case_id or case_id in cases:
            raise ValueError("Dataset case_id values must be non-empty and unique.")
        if case.get("mode") not in {"personal", "collected"}:
            raise ValueError(f"Dataset case {case_id} has an invalid mode.")
        if not isinstance(case.get("category"), str) or not case["category"].strip():
            raise ValueError(f"Dataset case {case_id} has an invalid category.")
        source_items = case.get("source_items")
        expected_groups = case.get("expected_groups")
        if not isinstance(source_items, list) or not source_items:
            raise ValueError(f"Dataset case {case_id} must contain source_items.")
        if not isinstance(expected_groups, list) or not expected_groups:
            raise ValueError(f"Dataset case {case_id} must contain expected_groups.")
        source_ids: list[str] = []
        for item in source_items:
            if not isinstance(item, dict):
                raise ValueError(f"Dataset case {case_id} has invalid source items.")
            source_id = item.get("source_id")
            source_text = item.get("text")
            if (
                not isinstance(source_id, str)
                or not source_id.strip()
                or source_id != source_id.strip()
                or not isinstance(source_text, str)
                or not source_text.strip()
            ):
                raise ValueError(f"Dataset case {case_id} has invalid source items.")
            source_ids.append(source_id)
        if len(source_ids) != len(set(source_ids)):
            raise ValueError(f"Dataset case {case_id} source ids must be unique.")
        for group in expected_groups:
            if (
                not isinstance(group, list)
                or not group
                or not all(
                    isinstance(source_id, str)
                    and bool(source_id.strip())
                    and source_id == source_id.strip()
                    for source_id in group
                )
            ):
                raise ValueError(
                    f"Dataset case {case_id} expected_groups must contain "
                    "non-empty string lists."
                )
        flat_expected = [source_id for group in expected_groups for source_id in group]
        if sorted(flat_expected) != sorted(source_ids):
            raise ValueError(
                f"Dataset case {case_id} expected_groups must cover each source once."
            )
        for field_name in (
            "forbidden_title_fragments",
            "forbidden_content_fragments",
        ):
            fragments = case.get(field_name)
            if not isinstance(fragments, list) or not all(
                isinstance(fragment, str) and bool(fragment)
                for fragment in fragments
            ):
                raise ValueError(
                    f"Dataset case {case_id} field {field_name} must be a string list."
                )
        cases[case_id] = case
    return cases


def _read_metadata(result: dict[str, Any]) -> dict[str, Any]:
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("Result field `metadata` must be an object.")
    missing = [key for key in REQUIRED_METADATA_FIELDS if key not in metadata]
    if missing:
        raise ValueError(f"Result metadata is missing fields: {missing}")
    for key in ("variant", "model", "service", "reasoning_effort"):
        if not isinstance(metadata[key], str) or not metadata[key].strip():
            raise ValueError(f"Result metadata field {key} must be a non-empty string.")
    if not isinstance(metadata["stream_enabled"], bool):
        raise ValueError("Result metadata field stream_enabled must be a boolean.")
    return {key: metadata[key] for key in REQUIRED_METADATA_FIELDS}


def _evaluate_case(case: dict[str, Any], result_case: dict[str, Any]) -> dict[str, Any]:
    case_id = case["case_id"]
    expected_ids = [item["source_id"] for item in case["source_items"]]
    expected_id_set = set(expected_ids)
    groups = result_case.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError(f"Result case {case_id} field `groups` must be non-empty.")
    actual_memberships: dict[str, set[int]] = {}
    assignment_counts: dict[str, int] = {}
    covered_ids: set[str] = set()
    unknown_assignment_count = 0
    invalid_fact_source_reference_count = 0
    forbidden_hits = 0
    for group_index, group in enumerate(groups):
        if not isinstance(group, dict):
            raise ValueError(f"Result case {case_id} groups must be objects.")
        member_ids = group.get("member_ids")
        fact_source_ids = group.get("fact_source_ids")
        title = group.get("title")
        content = group.get("content")
        if not isinstance(member_ids, list) or not member_ids:
            raise ValueError(f"Result case {case_id} has an empty member_ids list.")
        if not isinstance(fact_source_ids, list):
            raise ValueError(f"Result case {case_id} fact_source_ids must be a list.")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"Result case {case_id} title must be non-empty.")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"Result case {case_id} content must be non-empty.")
        for source_id in member_ids:
            if not isinstance(source_id, str) or not source_id.strip():
                raise ValueError(
                    f"Result case {case_id} member ids must be non-empty strings."
                )
            actual_memberships.setdefault(source_id, set()).add(group_index)
            assignment_counts[source_id] = assignment_counts.get(source_id, 0) + 1
            if source_id not in expected_id_set:
                unknown_assignment_count += 1
        member_id_set = set(member_ids)
        for source_id in fact_source_ids:
            if not isinstance(source_id, str) or not source_id.strip():
                raise ValueError(
                    f"Result case {case_id} fact source ids must be non-empty strings."
                )
            if source_id in expected_id_set and source_id in member_id_set:
                covered_ids.add(source_id)
            else:
                invalid_fact_source_reference_count += 1
        forbidden_hits += sum(
            fragment in title for fragment in case["forbidden_title_fragments"]
        )
        forbidden_hits += sum(
            fragment in content for fragment in case["forbidden_content_fragments"]
        )

    split_count = 0
    for expected_group in case["expected_groups"]:
        for left, right in combinations(expected_group, 2):
            if not actual_memberships.get(left, set()) & actual_memberships.get(
                right, set()
            ):
                split_count += 1
    merge_count = 0
    for left_group, right_group in combinations(case["expected_groups"], 2):
        for left in left_group:
            for right in right_group:
                if actual_memberships.get(left, set()) & actual_memberships.get(
                    right, set()
                ):
                    merge_count += 1
    missing_ids = [source_id for source_id in expected_ids if source_id not in actual_memberships]
    duplicate_count = sum(
        count - 1
        for source_id, count in assignment_counts.items()
        if source_id in expected_id_set and count > 1
    )
    missing_fact_ids = [
        source_id for source_id in expected_ids if source_id not in covered_ids
    ]
    metrics = _read_case_metrics(case_id, result_case)
    return {
        "case_id": case_id,
        "mode": case["mode"],
        "category": case["category"],
        "event_count": len(groups),
        "expected_source_count": len(expected_ids),
        "assigned_source_count": len(expected_id_set & set(actual_memberships)),
        "source_covered_count": len(covered_ids),
        "missing_source_assignment_count": len(missing_ids),
        "duplicate_source_assignment_count": duplicate_count,
        "unknown_source_assignment_count": unknown_assignment_count,
        "missing_fact_source_count": len(missing_fact_ids),
        "invalid_fact_source_reference_count": (
            invalid_fact_source_reference_count
        ),
        "incorrect_split_pair_count": split_count,
        "incorrect_merge_pair_count": merge_count,
        "forbidden_style_hit_count": forbidden_hits,
        **metrics,
    }


def _read_case_metrics(case_id: str, result_case: dict[str, Any]) -> dict[str, int]:
    metrics: dict[str, int] = {}
    for key in REQUIRED_METRIC_FIELDS:
        value = result_case.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(
                f"Result case {case_id} metric {key} must be a non-negative integer."
            )
        metrics[key] = value
    return metrics


def _validate_comparable_metadata(
    baseline: dict[str, Any],
    optimized: dict[str, Any],
) -> None:
    comparison_fields = (
        "model",
        "service",
        "reasoning_effort",
        "stream_enabled",
    )
    mismatches = [
        key for key in comparison_fields if baseline[key] != optimized[key]
    ]
    if mismatches:
        raise ValueError(
            "Baseline and optimized runs must use identical model settings: "
            + ", ".join(mismatches)
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare deterministic metrics for anonymized event-generation runs."
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--optimized", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = compare_results(
        load_json_object(args.dataset),
        load_json_object(args.baseline),
        load_json_object(args.optimized),
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
