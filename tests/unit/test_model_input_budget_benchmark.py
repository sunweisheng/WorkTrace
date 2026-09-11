from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import benchmark_model_input_budget as benchmark
from src.worktrace.config import ModelInputBudgetSelection, RuntimeConfig
from src.worktrace.models import DailyRunResult


DATASET_PATH = Path("tests/fixtures/event_generation_quality_cases.json")


def test_benchmark_script_help_works_from_repository_root() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/benchmark_model_input_budget.py",
            "--help",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--thresholds" in result.stdout
    assert "--fallback-thresholds" in result.stdout
    assert "--fallback-repeats" in result.stdout
    assert "--reuse-existing" in result.stdout
    assert "--reuse-primary-existing" in result.stdout
    assert "--resume-existing" in result.stdout


def _successful_groups(case: dict[str, object]) -> list[dict[str, object]]:
    return [
        {
            "member_ids": list(group),
            "title": f"{case['case_id']}完整事项",
            "content": "围绕共同对象形成了处理过程和结果。",
            "fact_source_ids": list(group),
        }
        for group in case["expected_groups"]
    ]


def _fake_run_factory(
    calls: list[tuple[str, int, int]],
    *,
    failing_primary_threshold: int | None = None,
):
    def fake_run_once(
        dataset,
        *,
        config,
        route,
        threshold,
        repeat_index,
        cwd,
    ):
        del config, cwd
        calls.append((route, threshold, repeat_index))
        failed = route == "primary" and threshold == failing_primary_threshold
        result_cases = [
            {
                "case_id": case["case_id"],
                "groups": _successful_groups(case),
                "input_estimated_tokens": 100,
                "model_call_count": 1,
                "retry_count": 0,
                "elapsed_ms": 10,
            }
            for case in dataset["cases"]
        ]
        result_payload = {
            "metadata": {
                "variant": f"{route}-{threshold}-{repeat_index}",
                "model": f"{route}-model",
                "service": route,
                "reasoning_effort": "test",
                "stream_enabled": False,
            },
            "cases": result_cases,
        }
        mode_results = [
            {
                "mode": mode,
                "status": "failed" if failed and mode == "personal" else "success",
                "python_validation_pass": not (failed and mode == "personal"),
                "failed_request_count": int(failed and mode == "personal"),
                "protocol_failure_count": 0,
                "timeout_failure_count": 0,
                "wall_clock_ms": 10.0,
            }
            for mode in ("personal", "collected")
        ]
        return {
            "route": route,
            "threshold": threshold,
            "repeat_index": repeat_index,
            "mode_results": mode_results,
            "result_payload": result_payload,
            "automatic_evaluation": benchmark.evaluate_result(
                dataset,
                result_payload,
            ),
        }

    return fake_run_once


def _matched_runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
        model_input_budget_selection=ModelInputBudgetSelection(
            profile_matched=True,
            profile_id="test-profile",
            target_tokens=7000,
            benchmark_dataset_version="test-dataset",
        )
    )


def _selection_inputs(
    *,
    candidate_wall_clock_ms: float,
    candidate_completeness: int = 4,
) -> tuple[
    list[dict[str, object]],
    dict[str, dict[str, object]],
    dict[str, dict[str, object]],
]:
    runs: list[dict[str, object]] = []
    mapping: dict[str, dict[str, object]] = {}
    scores: dict[str, dict[str, object]] = {}
    for threshold, wall_clock_ms in (
        (7000, 100.0),
        (20000, candidate_wall_clock_ms),
    ):
        for route in ("primary", "fallback"):
            runs.append(
                {
                    "threshold": threshold,
                    "route": route,
                    "mode_results": [
                        {"mode": "personal", "wall_clock_ms": wall_clock_ms / 2},
                        {"mode": "collected", "wall_clock_ms": wall_clock_ms / 2},
                    ],
                }
            )
            for mode in ("personal", "collected"):
                review_id = f"{threshold}-{route}-{mode}"
                mapping[review_id] = {
                    "threshold": threshold,
                    "route": route,
                    "mode": mode,
                    "repeat_index": 1,
                    "case_id": review_id,
                }
                scores[review_id] = {
                    "fact_accurate": True,
                    "completeness": (
                        candidate_completeness if threshold == 20000 else 4
                    ),
                    "readability": 4,
                }
    return runs, scores, mapping


def test_selection_prefers_lower_threshold_when_time_difference_is_under_ten_percent() -> None:
    runs, scores, mapping = _selection_inputs(candidate_wall_clock_ms=95.0)

    result = benchmark.select_threshold(runs, scores, mapping)

    assert result["selected_target_tokens"] == 7000


def test_selection_uses_faster_threshold_when_quality_matches_and_gain_is_clear() -> None:
    runs, scores, mapping = _selection_inputs(candidate_wall_clock_ms=80.0)

    result = benchmark.select_threshold(runs, scores, mapping)

    assert result["selected_target_tokens"] == 20000


def test_selection_prefers_better_completeness_before_time() -> None:
    runs, scores, mapping = _selection_inputs(
        candidate_wall_clock_ms=120.0,
        candidate_completeness=5,
    )

    result = benchmark.select_threshold(runs, scores, mapping)

    assert result["selected_target_tokens"] == 20000


def test_selection_rejects_any_threshold_with_inaccurate_fact() -> None:
    runs, scores, mapping = _selection_inputs(candidate_wall_clock_ms=50.0)
    scores["20000-primary-personal"]["fact_accurate"] = False

    result = benchmark.select_threshold(runs, scores, mapping)

    assert result["selected_target_tokens"] == 7000


def test_selection_can_compare_candidate_with_failed_automatic_baseline() -> None:
    runs, scores, mapping = _selection_inputs(candidate_wall_clock_ms=80.0)

    result = benchmark.select_threshold(
        runs,
        scores,
        mapping,
        eligible_thresholds={20000},
    )

    assert result["selected_target_tokens"] == 20000


def test_threshold_config_disables_all_delivery_and_trace_outputs() -> None:
    config = benchmark._config_for_threshold(RuntimeConfig(), 20000)

    assert config.model_input_batch_target_tokens == 20000
    assert config.self_delivery_enabled is False
    assert config.conversation_debug_root is None
    assert config.collected_merge_trace_enabled is False


def test_isolated_date_validation_uses_temporary_roots_and_removes_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(target_date: str, config: RuntimeConfig) -> DailyRunResult:
        captured["target_date"] = target_date
        captured["config"] = config
        output_path = config.data_root / "temporary.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("temporary", encoding="utf-8")
        captured["output_path"] = output_path
        return DailyRunResult(
            target_date=target_date,
            conversation_count=1,
            message_count=1,
            slice_count=1,
            batch_count=1,
            event_count=1,
            skipped_slice_count=0,
            warning_count=0,
            status="success",
            output_path=str(output_path),
            error_summary="",
            self_delivery_status="disabled",
            stage_timing_summary={
                "total": {
                    "wall_clock_ms": 1.0,
                    "request_accumulated_ms": 0.0,
                }
            },
        )

    monkeypatch.setattr(benchmark, "run_daily_trace", fake_run)
    output_dir = tmp_path / "benchmark"
    output_dir.mkdir()

    result = benchmark.run_isolated_date_validation(
        "2026-09-10",
        config=RuntimeConfig(model_input_batch_target_tokens=20000),
        output_dir=output_dir,
    )

    isolated_config = captured["config"]
    assert isinstance(isolated_config, RuntimeConfig)
    assert isolated_config.self_delivery_enabled is False
    assert isolated_config.data_root != RuntimeConfig().data_root
    assert isolated_config.cache_root is not None
    assert result["self_delivery_status"] == "disabled"
    assert result["output_was_written_in_temporary_directory"] is True
    assert not Path(captured["output_path"]).exists()


def test_benchmark_main_only_sends_primary_passing_thresholds_to_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(
        benchmark,
        "load_runtime_config_overrides",
        lambda config, cwd: _matched_runtime_config(),
    )

    benchmark.main(
        [
            "--dataset",
            str(DATASET_PATH),
            "--thresholds",
            "7000",
            "12000",
            "--repeats",
            "1",
            "--output-dir",
            str(tmp_path),
        ],
        run_once=_fake_run_factory(
            calls,
            failing_primary_threshold=12000,
        ),
    )

    assert calls == [
        ("primary", 7000, 1),
        ("primary", 12000, 1),
        ("fallback", 7000, 1),
    ]
    summary = json.loads(
        (tmp_path / "benchmark-summary.json").read_text(encoding="utf-8")
    )
    assert summary["primary_gates"]["7000"]["passed"] is True
    assert summary["primary_gates"]["12000"]["passed"] is False
    assert set(summary["fallback_gates"]) == {"7000"}
    saved_run = json.loads(
        (tmp_path / "primary-7000-repeat-1.json").read_text(encoding="utf-8")
    )
    assert saved_run["result_payload"]["cases"]


def test_benchmark_uses_two_fallback_thresholds_once_after_primary_repeats(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(
        benchmark,
        "load_runtime_config_overrides",
        lambda config, cwd: _matched_runtime_config(),
    )

    benchmark.main(
        [
            "--dataset",
            str(DATASET_PATH),
            "--thresholds",
            "7000",
            "12000",
            "20000",
            "--repeats",
            "2",
            "--output-dir",
            str(tmp_path),
        ],
        run_once=_fake_run_factory(calls),
    )

    assert calls == [
        ("primary", 7000, 1),
        ("primary", 7000, 2),
        ("primary", 12000, 1),
        ("primary", 12000, 2),
        ("primary", 20000, 1),
        ("primary", 20000, 2),
        ("fallback", 7000, 1),
        ("fallback", 20000, 1),
    ]
    summary = json.loads(
        (tmp_path / "benchmark-summary.json").read_text(encoding="utf-8")
    )
    assert summary["primary_repeats"] == 2
    assert summary["fallback_thresholds"] == [7000, 20000]
    assert summary["fallback_repeats"] == 1


def test_reuse_primary_existing_ignores_old_fallback_runs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    old_calls: list[tuple[str, int, int]] = []
    old_run_once = _fake_run_factory(old_calls)
    old_runs = [
        old_run_once(
            dataset,
            config=_matched_runtime_config(),
            route=route,
            threshold=threshold,
            repeat_index=repeat_index,
            cwd=tmp_path,
        )
        for route, threshold, repeat_index in (
            ("primary", 7000, 1),
            ("primary", 7000, 2),
            ("primary", 20000, 1),
            ("primary", 20000, 2),
            ("fallback", 7000, 1),
            ("fallback", 7000, 2),
            ("fallback", 20000, 1),
            ("fallback", 20000, 2),
        )
    ]
    (tmp_path / "benchmark-summary.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_version": dataset["dataset_version"],
                "model_input_budget_profile_id": "test-profile",
                "thresholds": [7000, 20000],
                "repeats": 2,
                "runs": old_runs,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        benchmark,
        "load_runtime_config_overrides",
        lambda config, cwd: _matched_runtime_config(),
    )
    calls: list[tuple[str, int, int]] = []

    benchmark.main(
        [
            "--dataset",
            str(DATASET_PATH),
            "--thresholds",
            "7000",
            "20000",
            "--repeats",
            "2",
            "--output-dir",
            str(tmp_path),
            "--reuse-primary-existing",
        ],
        run_once=_fake_run_factory(calls),
    )

    assert calls == [("fallback", 7000, 1), ("fallback", 20000, 1)]
    summary = json.loads(
        (tmp_path / "benchmark-summary.json").read_text(encoding="utf-8")
    )
    assert len(summary["runs"]) == 6
    assert [
        (run["route"], run["threshold"], run["repeat_index"])
        for run in summary["runs"]
        if run["route"] == "fallback"
    ] == [("fallback", 7000, 1), ("fallback", 20000, 1)]


def test_resume_existing_only_retries_failed_runs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    old_run_once = _fake_run_factory([])
    runs = [
        old_run_once(
            dataset,
            config=_matched_runtime_config(),
            route=route,
            threshold=threshold,
            repeat_index=repeat_index,
            cwd=tmp_path,
        )
        for route, threshold, repeat_index in (
            ("primary", 7000, 1),
            ("primary", 20000, 1),
            ("fallback", 7000, 1),
            ("fallback", 20000, 1),
        )
    ]
    runs[0]["mode_results"][0]["status"] = "failed"
    runs[0]["mode_results"][0]["python_validation_pass"] = False
    runs[2]["mode_results"][1]["status"] = "failed"
    runs[2]["mode_results"][1]["python_validation_pass"] = False
    (tmp_path / "benchmark-summary.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_version": dataset["dataset_version"],
                "model_input_budget_profile_id": "test-profile",
                "thresholds": [7000, 20000],
                "primary_repeats": 1,
                "repeats": 1,
                "fallback_thresholds": [7000, 20000],
                "fallback_repeats": 1,
                "runs": runs,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        benchmark,
        "load_runtime_config_overrides",
        lambda config, cwd: _matched_runtime_config(),
    )
    calls: list[tuple[str, int, int]] = []

    benchmark.main(
        [
            "--dataset",
            str(DATASET_PATH),
            "--thresholds",
            "7000",
            "20000",
            "--repeats",
            "1",
            "--fallback-thresholds",
            "7000",
            "20000",
            "--fallback-repeats",
            "1",
            "--output-dir",
            str(tmp_path),
            "--resume-existing",
        ],
        run_once=_fake_run_factory(calls),
    )

    assert calls == [("primary", 7000, 1), ("fallback", 7000, 1)]


def test_blind_review_does_not_expose_route_threshold_or_repeat(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(
        benchmark,
        "load_runtime_config_overrides",
        lambda config, cwd: _matched_runtime_config(),
    )

    benchmark.main(
        [
            "--dataset",
            str(DATASET_PATH),
            "--thresholds",
            "7000",
            "--repeats",
            "1",
            "--output-dir",
            str(tmp_path),
        ],
        run_once=_fake_run_factory(calls),
    )

    blind = json.loads((tmp_path / "blind-review.json").read_text(encoding="utf-8"))
    assert blind["items"]
    for item in blind["items"]:
        assert set(item) == {
            "review_id",
            "mode",
            "source_facts",
            "generated_groups",
        }
    assert "threshold" not in json.dumps(blind, ensure_ascii=False).lower()
    mapping = json.loads(
        (tmp_path / "blind-review-mapping.json").read_text(encoding="utf-8")
    )
    assert {item["threshold"] for item in mapping.values()} == {7000}


def test_apply_selection_requires_complete_manual_review_before_profile_update(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        benchmark,
        "load_runtime_config_overrides",
        lambda config, cwd: _matched_runtime_config(),
    )
    calls: list[tuple[str, int, int]] = []
    run_once = _fake_run_factory(calls)
    output_dir = tmp_path / "first"
    benchmark.main(
        [
            "--dataset",
            str(DATASET_PATH),
            "--thresholds",
            "7000",
            "--repeats",
            "1",
            "--output-dir",
            str(output_dir),
        ],
        run_once=run_once,
    )
    scores = json.loads(
        (output_dir / "manual-review-template.json").read_text(encoding="utf-8")
    )
    scores.pop(next(iter(scores)))
    score_path = tmp_path / "incomplete-review.json"
    score_path.write_text(json.dumps(scores), encoding="utf-8")
    applied: list[int] = []
    monkeypatch.setattr(
        benchmark,
        "_apply_profile_selection",
        lambda **kwargs: applied.append(int(kwargs["target_tokens"])),
    )

    with pytest.raises(ValueError, match="Missing manual review score"):
        benchmark.main(
            [
                "--dataset",
                str(DATASET_PATH),
                "--thresholds",
                "7000",
                "--repeats",
                "1",
                "--output-dir",
                str(output_dir),
                "--manual-review",
                str(score_path),
                "--apply-selection",
                "--isolate-date",
                "2026-09-10",
                "--reuse-existing",
            ],
            run_once=run_once,
        )

    assert applied == []


def test_apply_selection_requires_isolated_date_validation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        benchmark,
        "load_runtime_config_overrides",
        lambda config, cwd: _matched_runtime_config(),
    )
    output_dir = tmp_path / "benchmark"
    benchmark.main(
        [
            "--dataset",
            str(DATASET_PATH),
            "--thresholds",
            "7000",
            "--repeats",
            "1",
            "--output-dir",
            str(output_dir),
        ],
        run_once=_fake_run_factory([]),
    )
    scores = json.loads(
        (output_dir / "manual-review-template.json").read_text(encoding="utf-8")
    )
    for score in scores.values():
        score.update(fact_accurate=True, completeness=4, readability=4)
    score_path = tmp_path / "manual-review.json"
    score_path.write_text(json.dumps(scores), encoding="utf-8")

    with pytest.raises(ValueError, match="requires a successful --isolate-date"):
        benchmark.main(
            [
                "--dataset",
                str(DATASET_PATH),
                "--thresholds",
                "7000",
                "--repeats",
                "1",
                "--output-dir",
                str(output_dir),
                "--manual-review",
                str(score_path),
                "--apply-selection",
                "--reuse-existing",
            ],
            run_once=_fake_run_factory([]),
        )


def test_failed_isolated_date_validation_does_not_update_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        benchmark,
        "load_runtime_config_overrides",
        lambda config, cwd: _matched_runtime_config(),
    )
    output_dir = tmp_path / "benchmark"
    benchmark.main(
        [
            "--dataset",
            str(DATASET_PATH),
            "--thresholds",
            "7000",
            "--repeats",
            "1",
            "--output-dir",
            str(output_dir),
        ],
        run_once=_fake_run_factory([]),
    )
    scores = json.loads(
        (output_dir / "manual-review-template.json").read_text(encoding="utf-8")
    )
    for score in scores.values():
        score.update(fact_accurate=True, completeness=4, readability=4)
    score_path = tmp_path / "manual-review.json"
    score_path.write_text(json.dumps(scores), encoding="utf-8")
    actions: list[str] = []
    monkeypatch.setattr(
        benchmark,
        "run_isolated_date_validation",
        lambda *args, **kwargs: {
            "status": "failed",
            "self_delivery_status": "disabled",
            "output_was_written_in_temporary_directory": False,
        },
    )
    monkeypatch.setattr(
        benchmark,
        "_apply_profile_selection",
        lambda **kwargs: actions.append("profile_updated"),
    )

    with pytest.raises(RuntimeError, match="Isolated date validation did not pass"):
        benchmark.main(
            [
                "--dataset",
                str(DATASET_PATH),
                "--thresholds",
                "7000",
                "--repeats",
                "1",
                "--output-dir",
                str(output_dir),
                "--manual-review",
                str(score_path),
                "--apply-selection",
                "--isolate-date",
                "2026-09-10",
                "--reuse-existing",
            ],
            run_once=_fake_run_factory([]),
        )

    assert actions == []


def test_successful_isolated_date_validation_precedes_profile_update(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        benchmark,
        "load_runtime_config_overrides",
        lambda config, cwd: _matched_runtime_config(),
    )
    output_dir = tmp_path / "benchmark"
    benchmark.main(
        [
            "--dataset",
            str(DATASET_PATH),
            "--thresholds",
            "7000",
            "--repeats",
            "1",
            "--output-dir",
            str(output_dir),
        ],
        run_once=_fake_run_factory([]),
    )
    scores = json.loads(
        (output_dir / "manual-review-template.json").read_text(encoding="utf-8")
    )
    for score in scores.values():
        score.update(fact_accurate=True, completeness=4, readability=4)
    score_path = tmp_path / "manual-review.json"
    score_path.write_text(json.dumps(scores), encoding="utf-8")
    actions: list[str] = []

    def fake_isolated_validation(*args, **kwargs):
        actions.append("isolated_validation")
        return {
            "status": "success_with_warnings",
            "self_delivery_status": "disabled",
            "output_was_written_in_temporary_directory": True,
        }

    monkeypatch.setattr(
        benchmark,
        "run_isolated_date_validation",
        fake_isolated_validation,
    )
    monkeypatch.setattr(
        benchmark,
        "_apply_profile_selection",
        lambda **kwargs: actions.append("profile_updated"),
    )

    benchmark.main(
        [
            "--dataset",
            str(DATASET_PATH),
            "--thresholds",
            "7000",
            "--repeats",
            "1",
            "--output-dir",
            str(output_dir),
            "--manual-review",
            str(score_path),
            "--apply-selection",
            "--isolate-date",
            "2026-09-10",
            "--reuse-existing",
        ],
        run_once=_fake_run_factory([]),
    )

    assert actions == ["isolated_validation", "profile_updated"]


def test_apply_selection_without_manual_review_stops_before_model_runs(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, int, int]] = []

    with pytest.raises(ValueError, match="requires a completed"):
        benchmark.main(
            [
                "--dataset",
                str(DATASET_PATH),
                "--thresholds",
                "7000",
                "--repeats",
                "1",
                "--output-dir",
                str(tmp_path),
                "--apply-selection",
            ],
            run_once=_fake_run_factory(calls),
        )

    assert calls == []


def test_collected_empty_output_fails_python_validation(monkeypatch) -> None:
    class EmptyRunner:
        def _build_deterministic_groups(self, events):
            return [], []

        def _estimate_collected_grouping_prompt_tokens(self, *args):
            return 1

        def _merge_source_events_two_stage(self, target_date, events):
            return [], []

    monkeypatch.setattr(benchmark, "CollectedMergeRunner", lambda **kwargs: EmptyRunner())
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))

    result = benchmark._collected_benchmark(
        dataset,
        config=RuntimeConfig(model_input_batch_target_tokens=7000),
        analyzer=object(),
        recorder=benchmark.LLMUsageRecorder(),
    )

    assert result["status"] == "failed"
    assert result["python_validation_pass"] is False


def test_non_fact_padding_is_unique_and_keeps_source_fact_once() -> None:
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    first = dataset["cases"][0]["source_items"][0]
    second = dataset["cases"][0]["source_items"][1]

    first_text = benchmark._expanded_benchmark_context(
        dataset,
        source_id=first["source_id"],
        text=first["text"],
    )
    second_text = benchmark._expanded_benchmark_context(
        dataset,
        source_id=second["source_id"],
        text=second["text"],
    )

    assert first_text.count(first["text"]) == 1
    assert second_text.count(second["text"]) == 1
    assert first_text != second_text
    assert "不得写入标题" in first_text
    assert benchmark._strip_benchmark_non_fact_context(dataset, first_text) == first["text"]


def test_apply_profile_selection_updates_tokens_and_dataset_version(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "model_input_budget.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "default_target_tokens": 7000,
                "profiles": [
                    {
                        "profile_id": "test-profile",
                        "primary_model": "primary",
                        "fallback_model": "fallback",
                        "target_tokens": 7000,
                        "benchmark_dataset_version": "old-dataset",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    benchmark._apply_profile_selection(
        config_path=config_path,
        config=_matched_runtime_config(),
        target_tokens=20000,
        benchmark_dataset_version="new-dataset",
    )

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["profiles"][0]["target_tokens"] == 20000
    assert payload["profiles"][0]["benchmark_dataset_version"] == "new-dataset"


def test_isolated_date_validation_rejects_output_outside_temporary_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    outside_path = tmp_path / "formal-output.md"
    outside_path.write_text("must remain untouched", encoding="utf-8")

    def fake_run(target_date: str, config: RuntimeConfig) -> DailyRunResult:
        del config
        return DailyRunResult(
            target_date=target_date,
            conversation_count=1,
            message_count=1,
            slice_count=1,
            batch_count=1,
            event_count=1,
            skipped_slice_count=0,
            warning_count=0,
            status="success",
            output_path=str(outside_path),
            error_summary="",
            self_delivery_status="disabled",
        )

    monkeypatch.setattr(benchmark, "run_daily_trace", fake_run)

    with pytest.raises(RuntimeError, match="outside its temporary directory"):
        benchmark.run_isolated_date_validation(
            "2026-09-10",
            config=RuntimeConfig(model_input_batch_target_tokens=20000),
            output_dir=tmp_path,
        )

    assert outside_path.read_text(encoding="utf-8") == "must remain untouched"


def test_fallback_route_uses_single_production_fallback_attempt() -> None:
    analyzer = benchmark._build_route_analyzer(
        "fallback",
        config=RuntimeConfig(primary_request_retry_limit=1),
        cwd=Path.cwd(),
        recorder=benchmark.LLMUsageRecorder(),
    )

    assert analyzer.primary_request_retry_limit == 0
