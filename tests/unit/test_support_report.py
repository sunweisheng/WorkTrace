from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from src.worktrace.analyzers.function_calls import FunctionCallSpec
from src.worktrace.config import RuntimeConfig, load_runtime_config_overrides
from src.worktrace.constants import DailyRunStatus
from src.worktrace.errors import AnalyzerProtocolError
from src.worktrace.models import CollectedMergeRunResult, DailyRunResult
from src.worktrace.support_report import (
    AnalyzerBundle,
    build_diagnostic_facts,
    build_support_report_analyzers,
    generate_support_report,
    load_support_report_settings,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SAFE_ENVIRONMENT = {
    "worktrace_version": "3.2.0",
    "python_version": "3.12.1",
    "codex_version": "1.2.3",
    "lark_cli_version": "2.3.4",
    "system_type": "Darwin",
}


class QueueAnalyzer:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.specs: list[FunctionCallSpec] = []

    def request_function(
        self,
        prompt: str,
        *,
        function_spec: FunctionCallSpec,
        allow_oversized_input: bool = False,
    ) -> object:
        self.prompts.append(prompt)
        self.specs.append(function_spec)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _result(tmp_path: Path, *, status: str = DailyRunStatus.SUCCESS.value) -> DailyRunResult:
    output_path = tmp_path / "data" / "formal-output.md"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("formal", encoding="utf-8")
    return DailyRunResult(
        target_date="2026-06-22",
        conversation_count=2,
        message_count=8,
        slice_count=3,
        batch_count=1,
        event_count=2,
        skipped_slice_count=0,
        warning_count=1,
        status=status,
        output_path=str(output_path),
        error_summary=(
            "Alice /Users/alice oc_sensitive https://private.example "
            "example-model sk_private_value Network error"
        ),
        self_delivery_status="failed",
        self_delivery_target="ou_sensitive",
        self_delivery_error="send failed for 13800138000",
    )


def _valid_analysis(fact_id: str = "D001") -> dict[str, object]:
    return {
        "overall_assessment": "needs_attention",
        "findings": [
            {
                "category": "delivery",
                "severity": "medium",
                "fact_ids": [fact_id],
                "cause_ids": ["delivery_failed"],
                "user_check_ids": ["retry_delivery"],
                "product_suggestion_ids": ["improve_delivery_recovery"],
            }
        ],
    }


def _healthy_analysis(fact_id: str = "D001") -> dict[str, object]:
    return {
        "overall_assessment": "healthy",
        "findings": [
            {
                "category": "none",
                "severity": "info",
                "fact_ids": [fact_id],
                "cause_ids": ["no_issue_detected"],
                "user_check_ids": ["no_action"],
                "product_suggestion_ids": ["none"],
            }
        ],
    }


def test_support_report_uses_only_safe_facts_and_writes_one_markdown(tmp_path: Path) -> None:
    analyzer = QueueAnalyzer([_valid_analysis()])
    config = RuntimeConfig(data_root=tmp_path / "data")

    reference = generate_support_report(
        result=_result(tmp_path),
        run_mode="personal",
        config=config,
        cwd=REPO_ROOT,
        elapsed_ms=1234.5,
        analyzer_bundle=AnalyzerBundle(
            primary=analyzer,
            fallback=None,
            primary_kind="codex",
            online_request_retry_limit=0,
        ),
        environment=SAFE_ENVIRONMENT,
    )

    assert reference.status == "generated_with_llm"
    assert reference.privacy_check == "passed"
    assert reference.path is not None
    report_path = Path(reference.path)
    assert report_path.is_file()
    assert list(report_path.parent.glob("*.md")) == [report_path]
    assert not list(report_path.parent.glob("*.json"))
    assert not list(report_path.parent.glob("*.zip"))

    prompt = analyzer.prompts[0]
    report = report_path.read_text(encoding="utf-8")
    for private_value in (
        "Alice",
        "/Users/alice",
        "oc_sensitive",
        "ou_sensitive",
        "2026-06-22",
        "https://private.example",
        "example-model",
        "sk_private_value",
        "13800138000",
        "formal-output.md",
    ):
        assert private_value not in prompt
        assert private_value not in report
    assert "D001" in prompt
    assert "大模型问题分析" in report
    assert "结果送达" in report


def test_invalid_fact_ids_retry_online_then_use_codex(tmp_path: Path) -> None:
    invalid = _valid_analysis("D999")
    online = QueueAnalyzer([invalid, invalid])
    codex = QueueAnalyzer([_valid_analysis()])

    reference = generate_support_report(
        result=_result(tmp_path),
        run_mode="personal",
        config=RuntimeConfig(data_root=tmp_path / "data"),
        cwd=REPO_ROOT,
        elapsed_ms=100,
        analyzer_bundle=AnalyzerBundle(
            primary=online,
            fallback=codex,
            primary_kind="online",
            online_request_retry_limit=0,
        ),
        environment=SAFE_ENVIRONMENT,
    )

    assert reference.status == "generated_with_llm"
    assert len(online.prompts) == 2
    assert len(codex.prompts) == 1
    assert "fact_ids_invalid" in online.prompts[1]
    assert "D999" not in online.prompts[1]


def test_all_model_routes_failed_still_write_basic_markdown(tmp_path: Path) -> None:
    analyzer = QueueAnalyzer([AnalyzerProtocolError("model failed")])

    reference = generate_support_report(
        result=_result(tmp_path, status=DailyRunStatus.FAILED.value),
        run_mode="personal",
        config=RuntimeConfig(data_root=tmp_path / "data"),
        cwd=REPO_ROOT,
        elapsed_ms=200,
        analyzer_bundle=AnalyzerBundle(
            primary=analyzer,
            fallback=None,
            primary_kind="codex",
            online_request_retry_limit=0,
        ),
        environment=SAFE_ENVIRONMENT,
    )

    assert reference.status == "generated_after_llm_failure"
    assert reference.llm_status == "failed"
    assert reference.path is not None
    report = Path(reference.path).read_text(encoding="utf-8")
    assert "大模型整理失败" in report
    assert "D001" in report


def test_support_report_marks_disabled_self_delivery_as_disabled(
    tmp_path: Path,
) -> None:
    result = replace(
        _result(tmp_path),
        self_delivery_status="disabled",
        self_delivery_error="",
        error_summary="",
    )
    settings = load_support_report_settings(REPO_ROOT)

    facts = build_diagnostic_facts(
        result=result,
        run_mode="personal",
        config=RuntimeConfig(data_root=tmp_path / "data"),
        cwd=REPO_ROOT,
        elapsed_ms=100,
        settings=settings,
    )

    artifact_fact = next(fact for fact in facts if fact.kind == "artifact_status")
    assert artifact_fact.metrics["delivery_status"] == "disabled"


def test_privacy_scan_blocks_report_before_file_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    analyzer = QueueAnalyzer([_valid_analysis()])
    monkeypatch.setattr(
        "src.worktrace.support_report.render_support_report",
        lambda **_kwargs: "https://private.example\n",
    )

    reference = generate_support_report(
        result=_result(tmp_path),
        run_mode="personal",
        config=RuntimeConfig(data_root=tmp_path / "data"),
        cwd=REPO_ROOT,
        elapsed_ms=100,
        analyzer_bundle=AnalyzerBundle(
            primary=analyzer,
            fallback=None,
            primary_kind="codex",
            online_request_retry_limit=0,
        ),
        environment=SAFE_ENVIRONMENT,
    )

    assert reference.status == "blocked"
    assert reference.path is None
    assert reference.privacy_check == "failed"
    report_root = tmp_path / "data" / "debug" / "support_reports"
    assert not report_root.exists()


def test_collected_merge_facts_use_python_stage_and_usage_calculations(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "merged.md"
    output_path.write_text("merged", encoding="utf-8")
    trace_root = tmp_path / "trace"
    summary_path = trace_root / "2026-06-22" / "summary.json"
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text(
        json.dumps(
            {
                "llm_usage_summary": {
                    "request_count": 3,
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "total_tokens": 150,
                    "fallback_count": 1,
                    "by_backend": {
                        "online": {"failed_count": 1},
                        "codex": {"failed_count": 0},
                    },
                },
                "retry_count_by_reason": {"invalid_result": 2},
                "steps": [
                    {
                        "person_name": "Alice",
                        "input_dir": "/Users/alice/private",
                        "conversation_id": "oc_sensitive",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    result = CollectedMergeRunResult(
        status=DailyRunStatus.SUCCESS.value,
        target_date="2026-06-22",
        input_dir="/Users/alice/private",
        output_path=str(output_path),
        source_file_count=2,
        source_event_count=4,
        merged_event_count=3,
        skipped_file_count=0,
        self_delivery_status="success",
        stage_timing_summary={
            "candidate_grouping": {"wall_clock_ms": 750.0},
            "source_parse": {"wall_clock_ms": 250.0},
            "total": {"wall_clock_ms": 1000.0},
        },
    )
    settings = load_support_report_settings(REPO_ROOT)
    event_generation = load_runtime_config_overrides(
        RuntimeConfig(),
        cwd=REPO_ROOT,
    ).event_generation

    facts = build_diagnostic_facts(
        result=result,
        run_mode="collected_merge",
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            collected_merge_trace_root=trace_root,
            event_generation=event_generation,
        ),
        cwd=REPO_ROOT,
        elapsed_ms=9999,
        settings=settings,
    )

    stage_facts = [fact for fact in facts if fact.kind == "stage_timing"]
    assert stage_facts[0].metrics == {
        "stage": "candidate_grouping",
        "wall_clock_ms": 750.0,
        "share_percent": 75.0,
        "slow_stage_rank": 1,
        "is_slow_stage": 1,
    }
    assert stage_facts[1].metrics["share_percent"] == 25.0
    model_fact = next(fact for fact in facts if fact.kind == "model_usage")
    assert model_fact.metrics == {
        "request_count": 3,
        "token_reporting_status": "reported",
        "reported_token_request_count": 3,
        "unreported_token_request_count": 0,
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "retry_count": 2,
        "fallback_count": 1,
        "failed_request_count": 1,
        "oversized_request_count": 0,
        "max_input_estimated_tokens": 0,
    }
    generation_fact = next(
        fact for fact in facts if fact.kind == "event_generation_config"
    )
    assert generation_fact.metrics["schema_version"] == 1
    assert generation_fact.metrics["config_loaded"] is True
    assert generation_fact.metrics["personal_positive_example_count"] == 5
    assert generation_fact.metrics["collected_positive_example_count"] == 2
    serialized_generation_fact = json.dumps(
        generation_fact.to_dict(),
        ensure_ascii=False,
    )
    assert "source_facts" not in serialized_generation_fact
    assert "expected_output" not in serialized_generation_fact
    assert "Alice" not in json.dumps([fact.to_dict() for fact in facts])
    assert "oc_sensitive" not in json.dumps([fact.to_dict() for fact in facts])

    analyzer = QueueAnalyzer([_healthy_analysis()])
    reference = generate_support_report(
        result=result,
        run_mode="collected_merge",
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            collected_merge_trace_root=trace_root,
        ),
        cwd=REPO_ROOT,
        elapsed_ms=9999,
        analyzer_bundle=AnalyzerBundle(
            primary=analyzer,
            fallback=None,
            primary_kind="codex",
            online_request_retry_limit=0,
        ),
        environment=SAFE_ENVIRONMENT,
    )
    assert reference.status == "generated_with_llm"
    assert reference.path is not None
    assert Path(reference.path).is_file()


def test_success_with_warnings_does_not_create_runtime_error_fact(
    tmp_path: Path,
) -> None:
    result = replace(
        _result(tmp_path),
        status=DailyRunStatus.SUCCESS_WITH_WARNINGS.value,
        error_summary="oversized input was submitted as configured",
        self_delivery_status="success",
        self_delivery_error="",
    )
    facts = build_diagnostic_facts(
        result=result,
        run_mode="personal",
        config=RuntimeConfig(data_root=tmp_path / "data"),
        cwd=REPO_ROOT,
        elapsed_ms=100,
        settings=load_support_report_settings(REPO_ROOT),
    )

    assert not any(fact.kind == "error_category" for fact in facts)


def test_unreported_tokens_are_not_rendered_as_zero(tmp_path: Path) -> None:
    debug_root = tmp_path / "debug"
    date_root = debug_root / "2026-06-22"
    date_root.mkdir(parents=True)
    date_root.joinpath("llm_usage.json").write_text(
        json.dumps(
            {
                "usage": {
                    "request_count": 2,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "reported_input_tokens_request_count": 0,
                    "reported_output_tokens_request_count": 0,
                    "reported_total_tokens_request_count": 0,
                    "missing_input_tokens_request_count": 2,
                    "missing_output_tokens_request_count": 2,
                    "missing_total_tokens_request_count": 2,
                    "fallback_count": 0,
                    "by_backend": {},
                },
                "requests": [
                    {
                        "status": "success",
                        "token_usage_status": "unavailable",
                        "duration_ms": 20,
                        "estimated_input_tokens": 8100,
                        "oversized_singleton": True,
                    },
                    {
                        "status": "success",
                        "token_usage_status": "unavailable",
                        "duration_ms": 30,
                        "estimated_input_tokens": 6200,
                        "oversized_singleton": False,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    result = replace(
        _result(tmp_path),
        self_delivery_status="success",
        self_delivery_error="",
        error_summary="",
    )
    facts = build_diagnostic_facts(
        result=result,
        run_mode="personal",
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            conversation_debug_root=debug_root,
        ),
        cwd=REPO_ROOT,
        elapsed_ms=100,
        settings=load_support_report_settings(REPO_ROOT),
    )

    metrics = next(fact.metrics for fact in facts if fact.kind == "model_usage")
    assert metrics["token_reporting_status"] == "not_reported"
    assert metrics["reported_token_request_count"] == 0
    assert metrics["unreported_token_request_count"] == 2
    assert metrics["oversized_request_count"] == 1
    assert metrics["max_input_estimated_tokens"] == 8100
    assert "input_tokens" not in metrics
    assert "output_tokens" not in metrics
    assert "total_tokens" not in metrics


def test_missing_stage_breakdown_does_not_mark_total_as_slow(tmp_path: Path) -> None:
    result = replace(
        _result(tmp_path),
        self_delivery_status="success",
        self_delivery_error="",
        error_summary="",
        stage_timing_summary={},
    )
    facts = build_diagnostic_facts(
        result=result,
        run_mode="personal",
        config=RuntimeConfig(data_root=tmp_path / "data"),
        cwd=REPO_ROOT,
        elapsed_ms=100,
        settings=load_support_report_settings(REPO_ROOT),
    )

    assert not any(fact.kind == "stage_timing" for fact in facts)


def test_support_analysis_conflict_retries_then_uses_python_report(
    tmp_path: Path,
) -> None:
    conflict = {
        "overall_assessment": "needs_attention",
        "findings": [
            {
                "category": "runtime",
                "severity": "medium",
                "fact_ids": ["D001"],
                "cause_ids": ["runtime_failed"],
                "user_check_ids": ["review_output"],
                "product_suggestion_ids": ["improve_output_validation"],
            }
        ],
    }
    analyzer = QueueAnalyzer([conflict, conflict])
    result = replace(
        _result(tmp_path),
        status=DailyRunStatus.SUCCESS_WITH_WARNINGS.value,
        self_delivery_status="success",
        self_delivery_error="",
        error_summary="ordinary warning",
    )

    reference = generate_support_report(
        result=result,
        run_mode="personal",
        config=RuntimeConfig(data_root=tmp_path / "data"),
        cwd=REPO_ROOT,
        elapsed_ms=100,
        analyzer_bundle=AnalyzerBundle(
            primary=analyzer,
            fallback=None,
            primary_kind="codex",
            online_request_retry_limit=0,
        ),
        environment=SAFE_ENVIRONMENT,
    )

    assert reference.status == "generated_after_llm_failure"
    assert len(analyzer.prompts) == 2
    assert "runtime_status_conflict" in analyzer.prompts[1]


def test_missing_online_configuration_uses_codex_for_report(
    monkeypatch,
) -> None:
    def missing_settings(*_args, **_kwargs):
        raise ValueError("missing")

    monkeypatch.setattr(
        "src.worktrace.support_report.load_online_llm_settings",
        missing_settings,
    )

    bundle = build_support_report_analyzers(RuntimeConfig(), cwd=REPO_ROOT)

    assert bundle.primary_kind == "codex"
    assert bundle.fallback is None
    assert bundle.primary.__class__.__name__ == "CodexAnalyzer"
