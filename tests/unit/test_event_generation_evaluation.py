from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.evaluate_event_generation_quality import compare_results, evaluate_result


DATASET_PATH = Path("tests/fixtures/event_generation_quality_cases.json")


def _dataset() -> dict[str, object]:
    return json.loads(DATASET_PATH.read_text(encoding="utf-8"))


def _result(*, variant: str, introduce_errors: bool) -> dict[str, object]:
    dataset = _dataset()
    result_cases: list[dict[str, object]] = []
    for case in dataset["cases"]:
        groups = [
            {
                "member_ids": list(group),
                "title": f"{case['case_id']}完整事项",
                "content": "围绕共同对象连接处理过程和结果。",
                "fact_source_ids": list(group),
            }
            for group in case["expected_groups"]
        ]
        if introduce_errors and case["case_id"] == "personal_avoid_action_split":
            groups = [
                {
                    "member_ids": [source_id],
                    "title": f"动作{index}",
                    "content": "单独罗列动作。",
                    "fact_source_ids": [source_id],
                }
                for index, source_id in enumerate(
                    [item["source_id"] for item in case["source_items"]],
                    start=1,
                )
            ]
        if introduce_errors and case["case_id"] == "collected_avoid_wrong_merge":
            source_ids = [item["source_id"] for item in case["source_items"]]
            groups = [
                {
                    "member_ids": source_ids,
                    "title": "部门重点工作推进",
                    "content": "合并两个独立事项。",
                    "fact_source_ids": source_ids,
                }
            ]
        result_cases.append(
            {
                "case_id": case["case_id"],
                "groups": groups,
                "input_estimated_tokens": 1000,
                "model_call_count": 1,
                "retry_count": 0,
                "elapsed_ms": 100,
            }
        )
    return {
        "metadata": {
            "variant": variant,
            "model": "test-model",
            "service": "test-service",
            "reasoning_effort": "high",
            "stream_enabled": False,
        },
        "cases": result_cases,
    }


def test_anonymized_dataset_has_planned_case_coverage() -> None:
    dataset = _dataset()
    cases = dataset["cases"]

    assert dataset["dataset_version"] == "event-generation-quality-v1"
    assert dataset["benchmark_non_fact_context"]["repeat"] == 14
    assert "不得写入" in dataset["benchmark_non_fact_context"]["line_template"]
    assert len(cases) == 10
    assert sum(case["mode"] == "personal" for case in cases) == 6
    assert sum(case["mode"] == "collected" for case in cases) == 4
    assert sum(
        case["mode"] == "personal" and case["category"] == "complete_item"
        for case in cases
    ) == 4
    assert sum(
        case["mode"] == "collected" and case["category"] == "complete_item"
        for case in cases
    ) == 2
    raw_text = DATASET_PATH.read_text(encoding="utf-8")
    for source_value in (
        "陈之",
        "栗栋",
        "陈珏奇",
        "共享电单车9月需续保明细",
        "599辆",
        "1909个",
        "1298个",
    ):
        assert source_value not in raw_text


def test_evaluator_counts_split_merge_coverage_and_runtime_metrics() -> None:
    report = evaluate_result(
        _dataset(),
        _result(variant="baseline", introduce_errors=True),
    )
    summary = report["summary"]

    assert summary["case_count"] == 10
    assert summary["event_count"] == 13
    assert summary["incorrect_split_pair_count"] == 3
    assert summary["incorrect_merge_pair_count"] == 1
    assert summary["forbidden_style_hit_count"] == 1
    assert summary["source_coverage_rate"] == 1.0
    assert summary["model_call_count"] == 10
    assert summary["retry_count"] == 0
    assert summary["elapsed_ms"] == 1000
    assert summary["input_estimated_tokens_total"] == 10000


def test_evaluator_rejects_fractional_version_and_invalid_expected_group() -> None:
    dataset = _dataset()
    dataset["schema_version"] = 1.0

    with pytest.raises(ValueError, match="schema_version"):
        evaluate_result(dataset, _result(variant="baseline", introduce_errors=False))

    dataset = _dataset()
    dataset["cases"][0]["expected_groups"] = [42]

    with pytest.raises(ValueError, match="non-empty string lists"):
        evaluate_result(dataset, _result(variant="baseline", introduce_errors=False))


def test_evaluator_rejects_duplicate_dataset_source_ids() -> None:
    dataset = _dataset()
    first_case = dataset["cases"][0]
    first_case["source_items"][1]["source_id"] = first_case["source_items"][0][
        "source_id"
    ]

    with pytest.raises(ValueError, match="source ids must be unique"):
        evaluate_result(dataset, _result(variant="baseline", introduce_errors=False))


def test_evaluator_separates_assignment_and_fact_source_errors() -> None:
    result = _result(variant="baseline", introduce_errors=False)
    first_result = result["cases"][0]
    first_result["groups"] = [
        {
            "member_ids": ["p1-f1", "p1-f1", "unknown-source"],
            "title": "范围核对",
            "content": "完成范围核对。",
            "fact_source_ids": ["p1-f2", "unknown-source"],
        },
        {
            "member_ids": ["p1-f2"],
            "title": "申请提交",
            "content": "提交续签申请。",
            "fact_source_ids": [],
        },
    ]

    report = evaluate_result(_dataset(), result)
    first_case = next(
        case for case in report["cases"] if case["case_id"] == "personal_service_renewal"
    )

    assert first_case["missing_source_assignment_count"] == 1
    assert first_case["duplicate_source_assignment_count"] == 1
    assert first_case["unknown_source_assignment_count"] == 1
    assert first_case["missing_fact_source_count"] == 3
    assert first_case["invalid_fact_source_reference_count"] == 2
    assert first_case["source_covered_count"] == 0
    assert report["summary"]["fixed_format_pass"] is False


def test_comparison_requires_same_model_settings_and_reports_delta() -> None:
    baseline = _result(variant="baseline", introduce_errors=True)
    optimized = _result(variant="optimized", introduce_errors=False)

    report = compare_results(_dataset(), baseline, optimized)

    assert report["optimized"]["summary"]["automatic_quality_issue_count"] == 0
    assert report["delta_optimized_minus_baseline"][
        "automatic_quality_issue_count"
    ] == -5
    assert report["manual_review_required"] == [
        "事实准确性",
        "事项完整性",
        "可读性",
    ]

    optimized["metadata"]["model"] = "another-model"
    with pytest.raises(ValueError, match="identical model settings"):
        compare_results(_dataset(), baseline, optimized)
