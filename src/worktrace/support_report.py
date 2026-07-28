from __future__ import annotations

import json
import platform
import re
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import __version__
from .analyzers.function_calls import FunctionCallSpec
from .config import RuntimeConfig, load_online_llm_settings
from .errors import AnalyzerProtocolError, RetryableAnalyzerProtocolError
from .models import (
    CollectedMergeRunResult,
    DailyRunResult,
    SupportReportReference,
)


SUPPORT_REPORT_CONFIG_PATH = Path("config") / "support_report.json"
SUPPORT_REPORT_SCHEMA_VERSION = 1
_MAX_DEBUG_JSON_BYTES = 32 * 1024 * 1024
_VERSION_PATTERN = re.compile(r"\b\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9.]+)?\b")
_SAFE_KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class DiagnosticFact:
    fact_id: str
    kind: str
    metrics: dict[str, int | float | str]

    def to_dict(self) -> dict[str, object]:
        return {
            "fact_id": self.fact_id,
            "kind": self.kind,
            "metrics": dict(self.metrics),
        }


@dataclass(frozen=True)
class SupportFinding:
    category: str
    severity: str
    fact_ids: tuple[str, ...]
    cause_ids: tuple[str, ...]
    user_check_ids: tuple[str, ...]
    product_suggestion_ids: tuple[str, ...]


@dataclass(frozen=True)
class SupportAnalysis:
    overall_assessment: str
    findings: tuple[SupportFinding, ...]


@dataclass(frozen=True)
class AnalyzerBundle:
    primary: object
    fallback: object | None
    primary_kind: str
    online_request_retry_limit: int


@dataclass(frozen=True)
class SupportReportSettings:
    payload: dict[str, object]
    schema_version: int
    function_name: str
    function_description: str
    analysis_rules: tuple[str, ...]
    llm_validation_retry_limit: int
    slow_stage_min_share: float
    slow_stage_limit: int
    max_findings: int
    safe_text: dict[str, str]
    section_labels: dict[str, str]
    field_labels: dict[str, str]
    metric_labels: dict[str, str]
    fact_type_labels: dict[str, str]
    value_labels: dict[str, str]
    definitions: dict[str, tuple[dict[str, str], ...]]
    error_categories: tuple[dict[str, object], ...]
    privacy_patterns: tuple[tuple[str, re.Pattern[str]], ...]

    def definition_keys(self, name: str) -> tuple[str, ...]:
        return tuple(item["key"] for item in self.definitions[name])

    def definition_label(self, name: str, key: str) -> str:
        for item in self.definitions[name]:
            if item["key"] == key:
                return item["label"]
        return self.safe_text["unknown"]


def load_support_report_settings(cwd: Path) -> SupportReportSettings:
    config_path = cwd / SUPPORT_REPORT_CONFIG_PATH
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("Support report configuration is missing.") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("Support report configuration is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Support report configuration must contain an object.")

    schema_version = _required_int(payload, "schema_version", minimum=1)
    if schema_version != SUPPORT_REPORT_SCHEMA_VERSION:
        raise ValueError("Unsupported support report schema version.")
    function = _required_dict(payload, "function")
    function_name = _required_string(function, "name")
    if not _SAFE_KEY_PATTERN.fullmatch(function_name):
        raise ValueError("Support report Function name is invalid.")
    function_description = _required_string(function, "description")
    definitions_payload = _required_dict(payload, "definitions")
    definition_names = (
        "overall_assessments",
        "problem_categories",
        "severities",
        "possible_causes",
        "user_checks",
        "product_suggestions",
    )
    definitions = {
        name: _definition_list(definitions_payload, name)
        for name in definition_names
    }
    error_categories = _error_category_list(payload)
    privacy_patterns = _privacy_pattern_list(payload)
    return SupportReportSettings(
        payload=payload,
        schema_version=schema_version,
        function_name=function_name,
        function_description=function_description,
        analysis_rules=_string_list(payload, "analysis_rules"),
        llm_validation_retry_limit=_required_int(
            payload, "llm_validation_retry_limit", minimum=0
        ),
        slow_stage_min_share=_required_number(
            payload, "slow_stage_min_share", minimum=0, maximum=1
        ),
        slow_stage_limit=_required_int(payload, "slow_stage_limit", minimum=1),
        max_findings=_required_int(payload, "max_findings", minimum=1),
        safe_text=_string_dict(payload, "safe_text"),
        section_labels=_string_dict(payload, "section_labels"),
        field_labels=_string_dict(payload, "field_labels"),
        metric_labels=_string_dict(payload, "metric_labels"),
        fact_type_labels=_string_dict(payload, "fact_type_labels"),
        value_labels=_string_dict(payload, "value_labels"),
        definitions=definitions,
        error_categories=error_categories,
        privacy_patterns=privacy_patterns,
    )


def generate_support_report(
    *,
    result: DailyRunResult | CollectedMergeRunResult,
    run_mode: str,
    config: RuntimeConfig,
    cwd: Path,
    elapsed_ms: float,
    analyzer_bundle: AnalyzerBundle | None = None,
    environment: Mapping[str, str] | None = None,
) -> SupportReportReference:
    try:
        settings = load_support_report_settings(cwd)
        facts = build_diagnostic_facts(
            result=result,
            run_mode=run_mode,
            config=config,
            cwd=cwd,
            elapsed_ms=elapsed_ms,
            settings=settings,
        )
        bundle = analyzer_bundle or build_support_report_analyzers(config, cwd=cwd)
        analysis = _request_support_analysis(bundle, facts=facts, settings=settings)
        llm_status = "success" if analysis is not None else "failed"
        report_text = render_support_report(
            facts=facts,
            analysis=analysis,
            llm_status=llm_status,
            settings=settings,
            environment=dict(environment or collect_environment_versions()),
        )
        privacy_errors = scan_support_report_privacy(report_text, settings=settings)
        if privacy_errors:
            return SupportReportReference(
                status="blocked",
                path=None,
                llm_status=llm_status,
                privacy_check="failed",
                schema_version=settings.schema_version,
            )
        report_path = _write_support_report(
            report_text,
            config=config,
            cwd=cwd,
        )
        return SupportReportReference(
            status=(
                "generated_with_llm"
                if analysis is not None
                else "generated_after_llm_failure"
            ),
            path=_display_path(report_path, cwd=cwd),
            llm_status=llm_status,
            privacy_check="passed",
            schema_version=settings.schema_version,
        )
    except Exception:
        return SupportReportReference(
            status="failed",
            path=None,
            llm_status="failed",
            privacy_check="not_run",
            schema_version=SUPPORT_REPORT_SCHEMA_VERSION,
        )


def build_support_report_analyzers(
    config: RuntimeConfig,
    *,
    cwd: Path,
) -> AnalyzerBundle:
    from .analyzers.codex import CodexAnalyzer
    from .llm_usage import LLMUsageRecorder

    recorder = LLMUsageRecorder()
    codex = CodexAnalyzer(config=config, cwd=cwd, usage_recorder=recorder)
    if config.analyzer_backend != "online":
        return AnalyzerBundle(
            primary=codex,
            fallback=None,
            primary_kind="codex",
            online_request_retry_limit=0,
        )
    try:
        load_online_llm_settings(config, cwd=cwd)
    except ValueError:
        return AnalyzerBundle(
            primary=codex,
            fallback=None,
            primary_kind="codex",
            online_request_retry_limit=0,
        )

    from .analyzers.online import OnlineLLMAnalyzer

    online = OnlineLLMAnalyzer(config=config, cwd=cwd, usage_recorder=recorder)
    return AnalyzerBundle(
        primary=online,
        fallback=codex,
        primary_kind="online",
        online_request_retry_limit=config.online_request_retry_limit,
    )


def build_diagnostic_facts(
    *,
    result: DailyRunResult | CollectedMergeRunResult,
    run_mode: str,
    config: RuntimeConfig,
    cwd: Path,
    elapsed_ms: float,
    settings: SupportReportSettings,
) -> list[DiagnosticFact]:
    facts: list[DiagnosticFact] = []

    def add(kind: str, metrics: dict[str, int | float | str]) -> None:
        facts.append(
            DiagnosticFact(
                fact_id=f"D{len(facts) + 1:03d}",
                kind=kind,
                metrics=metrics,
            )
        )

    normalized_mode = run_mode if run_mode in {"personal", "collected_merge"} else "unknown"
    normalized_status = _known_value(result.status, settings=settings)
    if isinstance(result, DailyRunResult):
        add(
            "run_summary",
            {
                "run_mode": normalized_mode,
                "run_status": normalized_status,
                "conversation_count": _safe_count(result.conversation_count),
                "message_count": _safe_count(result.message_count),
                "slice_count": _safe_count(result.slice_count),
                "batch_count": _safe_count(result.batch_count),
                "event_count": _safe_count(result.event_count),
                "warning_count": _safe_count(result.warning_count),
                "skipped_count": _safe_count(result.skipped_slice_count),
            },
        )
        stage_summary: Mapping[str, object] = {}
        error_text = f"{result.error_summary}\n{result.self_delivery_error}"
    else:
        add(
            "run_summary",
            {
                "run_mode": normalized_mode,
                "run_status": normalized_status,
                "source_file_count": _safe_count(result.source_file_count),
                "source_event_count": _safe_count(result.source_event_count),
                "merged_event_count": _safe_count(result.merged_event_count),
                "warning_count": _safe_count(len(result.warning_messages)),
                "skipped_count": _safe_count(
                    result.skipped_file_count + result.partial_file_count
                ),
            },
        )
        stage_summary = result.stage_timing_summary
        error_text = "\n".join(
            [*result.warning_messages, result.self_delivery_error]
        )

    stage_rows, total_ms = _calculate_stage_rows(
        stage_summary,
        elapsed_ms=elapsed_ms,
        settings=settings,
    )
    add(
        "timing_summary",
        {
            "stage_count": len(stage_rows),
            "wall_clock_ms": total_ms,
        },
    )
    for row in stage_rows:
        add("stage_timing", row)

    usage = _load_safe_usage_summary(
        result=result,
        run_mode=normalized_mode,
        config=config,
        cwd=cwd,
    )
    add("model_usage", usage)

    output_path = result.output_path
    add(
        "artifact_status",
        {
            "write_status": (
                "written" if _output_exists(output_path, cwd=cwd) else "not_written"
            ),
            "delivery_status": _normalize_delivery_status(
                result.self_delivery_status,
                settings=settings,
            ),
        },
    )
    if normalized_status in {"failed", "invalid_input"} or error_text.strip():
        add(
            "error_category",
            {
                "error_category": classify_error_category(
                    error_text,
                    settings=settings,
                )
            },
        )
    return facts


def _calculate_stage_rows(
    stage_summary: Mapping[str, object],
    *,
    elapsed_ms: float,
    settings: SupportReportSettings,
) -> tuple[list[dict[str, int | float | str]], float]:
    safe_total = round(max(float(elapsed_ms), 0.0), 3)
    total_metrics = stage_summary.get("total")
    if isinstance(total_metrics, Mapping):
        configured_total = _safe_nonnegative_number(total_metrics.get("wall_clock_ms"))
        if configured_total is not None and configured_total > 0:
            safe_total = configured_total

    values: list[tuple[str, float]] = []
    for stage, raw_metrics in stage_summary.items():
        if stage == "total" or stage not in settings.value_labels:
            continue
        if not isinstance(raw_metrics, Mapping):
            continue
        duration = _safe_nonnegative_number(raw_metrics.get("wall_clock_ms"))
        if duration is not None:
            values.append((stage, duration))
    if not values:
        values = [("total", safe_total)]
    values.sort(key=lambda item: (-item[1], item[0]))
    rows: list[dict[str, int | float | str]] = []
    for rank, (stage, duration) in enumerate(values, start=1):
        share = round((duration / safe_total * 100) if safe_total > 0 else 0.0, 2)
        rows.append(
            {
                "stage": stage,
                "wall_clock_ms": duration,
                "share_percent": share,
                "slow_stage_rank": rank,
                "is_slow_stage": int(
                    rank <= settings.slow_stage_limit
                    and share >= settings.slow_stage_min_share * 100
                ),
            }
        )
    return rows, safe_total


def _load_safe_usage_summary(
    *,
    result: DailyRunResult | CollectedMergeRunResult,
    run_mode: str,
    config: RuntimeConfig,
    cwd: Path,
) -> dict[str, int]:
    summaries: list[Mapping[str, object]] = []
    request_records: list[Mapping[str, object]] = []
    explicit_retry_count = 0
    if run_mode == "personal":
        debug_root = config.conversation_debug_root or (
            config.data_root / "debug" / "conversations"
        )
        debug_root = _absolute_path(debug_root, cwd=cwd)
        payload = _read_debug_json(debug_root / result.target_date / "llm_usage.json")
        usage = payload.get("usage")
        if isinstance(usage, Mapping):
            summaries.append(usage)
        raw_requests = payload.get("requests")
        if isinstance(raw_requests, list):
            request_records.extend(
                item for item in raw_requests if isinstance(item, Mapping)
            )
    elif run_mode == "collected_merge":
        trace_root = _absolute_path(config.collected_merge_trace_root, cwd=cwd)
        date_root = trace_root / result.target_date
        if date_root.is_dir():
            for summary_path in sorted(date_root.rglob("summary.json")):
                payload = _read_debug_json(summary_path)
                usage = payload.get("llm_usage_summary")
                if isinstance(usage, Mapping):
                    summaries.append(usage)
                retry_counts = payload.get("retry_count_by_reason")
                if isinstance(retry_counts, Mapping):
                    explicit_retry_count += sum(
                        _safe_count(value) for value in retry_counts.values()
                    )

    request_count = sum(_mapping_count(item, "request_count") for item in summaries)
    input_tokens = sum(_mapping_count(item, "input_tokens") for item in summaries)
    output_tokens = sum(_mapping_count(item, "output_tokens") for item in summaries)
    total_tokens = sum(_mapping_count(item, "total_tokens") for item in summaries)
    fallback_count = sum(_mapping_count(item, "fallback_count") for item in summaries)
    failed_request_count = sum(_failed_backend_count(item) for item in summaries)
    record_failed_count = sum(
        item.get("status") == "failed" for item in request_records
    )
    retry_count = explicit_retry_count or sum(
        item.get("status") == "failed" and item.get("backend") == "online"
        for item in request_records
    )
    return {
        "request_count": request_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "retry_count": int(retry_count),
        "fallback_count": fallback_count,
        "failed_request_count": max(failed_request_count, record_failed_count),
    }


def classify_error_category(
    error_text: str,
    *,
    settings: SupportReportSettings,
) -> str:
    lowered = error_text.lower()
    for item in settings.error_categories:
        patterns = item["patterns"]
        if isinstance(patterns, tuple) and any(
            re.search(pattern, lowered) for pattern in patterns
        ):
            return str(item["key"])
    return "runtime"


def _request_support_analysis(
    bundle: AnalyzerBundle,
    *,
    facts: Sequence[DiagnosticFact],
    settings: SupportReportSettings,
) -> SupportAnalysis | None:
    spec = _support_function_spec(facts=facts, settings=settings)
    prompt = _support_prompt(facts=facts, settings=settings)
    if scan_support_report_privacy(prompt, settings=settings):
        return None
    if bundle.primary_kind != "online":
        try:
            return _request_and_validate(
                bundle.primary,
                prompt=prompt,
                spec=spec,
                facts=facts,
                settings=settings,
            )[0]
        except Exception:
            return None

    technical_failures = 0
    validation_failures = 0
    while True:
        try:
            analysis, validation_codes = _request_and_validate(
                bundle.primary,
                prompt=prompt,
                spec=spec,
                facts=facts,
                settings=settings,
            )
        except RetryableAnalyzerProtocolError:
            if technical_failures < bundle.online_request_retry_limit:
                technical_failures += 1
                continue
            break
        except Exception:
            break
        if analysis is not None:
            return analysis
        if validation_failures >= settings.llm_validation_retry_limit:
            break
        validation_failures += 1
        prompt = _support_prompt(
            facts=facts,
            settings=settings,
            validation_feedback=validation_codes,
        )

    if bundle.fallback is None:
        return None
    try:
        return _request_and_validate(
            bundle.fallback,
            prompt=prompt,
            spec=spec,
            facts=facts,
            settings=settings,
        )[0]
    except Exception:
        return None


def _request_and_validate(
    analyzer: object,
    *,
    prompt: str,
    spec: FunctionCallSpec,
    facts: Sequence[DiagnosticFact],
    settings: SupportReportSettings,
) -> tuple[SupportAnalysis | None, tuple[str, ...]]:
    request_function = getattr(analyzer, "request_function")
    payload = request_function(prompt, function_spec=spec)
    return validate_support_analysis(payload, facts=facts, settings=settings)


def _support_prompt(
    *,
    facts: Sequence[DiagnosticFact],
    settings: SupportReportSettings,
    validation_feedback: Sequence[str] = (),
) -> str:
    payload: dict[str, object] = {
        "task": "worktrace_support_analysis",
        "schema_version": settings.schema_version,
        "rules": list(settings.analysis_rules),
        "allowed_options": settings.definitions,
        "diagnostic_facts": [fact.to_dict() for fact in facts],
    }
    if validation_feedback:
        payload["previous_validation_errors"] = list(validation_feedback)
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _support_function_spec(
    *,
    facts: Sequence[DiagnosticFact],
    settings: SupportReportSettings,
) -> FunctionCallSpec:
    fact_ids = [fact.fact_id for fact in facts]
    properties = {
        "category": _enum_schema(settings.definition_keys("problem_categories")),
        "severity": _enum_schema(settings.definition_keys("severities")),
        "fact_ids": _array_enum_schema(fact_ids),
        "cause_ids": _array_enum_schema(settings.definition_keys("possible_causes")),
        "user_check_ids": _array_enum_schema(settings.definition_keys("user_checks")),
        "product_suggestion_ids": _array_enum_schema(
            settings.definition_keys("product_suggestions")
        ),
    }
    finding_schema = {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }
    parameters: dict[str, object] = {
        "type": "object",
        "properties": {
            "overall_assessment": _enum_schema(
                settings.definition_keys("overall_assessments")
            ),
            "findings": {
                "type": "array",
                "items": finding_schema,
                "minItems": 1,
                "maxItems": settings.max_findings,
            },
        },
        "required": ["overall_assessment", "findings"],
        "additionalProperties": False,
    }
    typical_fact_id = fact_ids[0]
    return FunctionCallSpec(
        request_kind="support_report",
        name=settings.function_name,
        description=settings.function_description,
        parameters=parameters,
        typical_arguments={
            "overall_assessment": settings.definition_keys("overall_assessments")[0],
            "findings": [
                {
                    "category": settings.definition_keys("problem_categories")[0],
                    "severity": settings.definition_keys("severities")[0],
                    "fact_ids": [typical_fact_id],
                    "cause_ids": [settings.definition_keys("possible_causes")[0]],
                    "user_check_ids": [settings.definition_keys("user_checks")[0]],
                    "product_suggestion_ids": [
                        settings.definition_keys("product_suggestions")[0]
                    ],
                }
            ],
        },
        final_parameter_checks=settings.analysis_rules,
    )


def validate_support_analysis(
    payload: object,
    *,
    facts: Sequence[DiagnosticFact],
    settings: SupportReportSettings,
) -> tuple[SupportAnalysis | None, tuple[str, ...]]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return None, ("payload_not_object",)
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if scan_support_report_privacy(serialized, settings=settings):
        return None, ("privacy_content_invalid",)
    if set(payload) != {"overall_assessment", "findings"}:
        errors.append("top_level_fields_invalid")
    overall = payload.get("overall_assessment")
    if overall not in settings.definition_keys("overall_assessments"):
        errors.append("overall_assessment_invalid")
    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list) or not (
        1 <= len(raw_findings) <= settings.max_findings
    ):
        errors.append("findings_count_invalid")
        raw_findings = []

    allowed_fact_ids = {fact.fact_id for fact in facts}
    findings: list[SupportFinding] = []
    expected_fields = {
        "category",
        "severity",
        "fact_ids",
        "cause_ids",
        "user_check_ids",
        "product_suggestion_ids",
    }
    for item in raw_findings:
        if not isinstance(item, dict) or set(item) != expected_fields:
            errors.append("finding_fields_invalid")
            continue
        category = item.get("category")
        severity = item.get("severity")
        fact_ids = _validated_id_list(item.get("fact_ids"), allowed_fact_ids)
        cause_ids = _validated_id_list(
            item.get("cause_ids"),
            set(settings.definition_keys("possible_causes")),
        )
        user_check_ids = _validated_id_list(
            item.get("user_check_ids"),
            set(settings.definition_keys("user_checks")),
        )
        product_suggestion_ids = _validated_id_list(
            item.get("product_suggestion_ids"),
            set(settings.definition_keys("product_suggestions")),
        )
        if category not in settings.definition_keys("problem_categories"):
            errors.append("category_invalid")
        if severity not in settings.definition_keys("severities"):
            errors.append("severity_invalid")
        if fact_ids is None:
            errors.append("fact_ids_invalid")
        if cause_ids is None:
            errors.append("cause_ids_invalid")
        if user_check_ids is None:
            errors.append("user_check_ids_invalid")
        if product_suggestion_ids is None:
            errors.append("product_suggestion_ids_invalid")
        if any(
            value is None
            for value in (
                fact_ids,
                cause_ids,
                user_check_ids,
                product_suggestion_ids,
            )
        ) or category not in settings.definition_keys(
            "problem_categories"
        ) or severity not in settings.definition_keys("severities"):
            continue
        findings.append(
            SupportFinding(
                category=str(category),
                severity=str(severity),
                fact_ids=tuple(fact_ids or ()),
                cause_ids=tuple(cause_ids or ()),
                user_check_ids=tuple(user_check_ids or ()),
                product_suggestion_ids=tuple(product_suggestion_ids or ()),
            )
        )
    if errors:
        return None, tuple(dict.fromkeys(errors))
    return (
        SupportAnalysis(
            overall_assessment=str(overall),
            findings=tuple(findings),
        ),
        (),
    )


def render_support_report(
    *,
    facts: Sequence[DiagnosticFact],
    analysis: SupportAnalysis | None,
    llm_status: str,
    settings: SupportReportSettings,
    environment: Mapping[str, str],
) -> str:
    sections = settings.section_labels
    fields = settings.field_labels
    text = settings.safe_text
    lines = [f"# {text['title']}", ""]
    lines.extend(
        [
            f"## {sections['privacy']}",
            "",
            f"- {fields['privacy_check']}：{settings.value_labels['passed']}",
            f"- {text['privacy_summary']}",
            f"- {text['share_notice']}",
            "",
            f"## {sections['environment']}",
            "",
            "| " + fields["metrics"] + " | " + fields["value"] + " |",
            "| --- | --- |",
        ]
    )
    for key in (
        "worktrace_version",
        "python_version",
        "codex_version",
        "lark_cli_version",
        "system_type",
    ):
        raw_value = str(environment.get(key, text["unknown"]))
        value = (
            settings.value_labels.get(raw_value, text["unknown"])
            if key == "system_type"
            else _safe_version(raw_value)
        )
        lines.append(f"| {fields[key]} | {_escape_markdown(value)} |")

    section_fact_kinds = (
        ("overview", {"run_summary", "timing_summary"}),
        ("stages", {"stage_timing"}),
        ("model_calls", {"model_usage"}),
        ("artifacts", {"artifact_status", "error_category"}),
    )
    for section_key, kinds in section_fact_kinds:
        selected = [fact for fact in facts if fact.kind in kinds]
        lines.extend(["", f"## {sections[section_key]}", ""])
        if not selected and section_key == "stages":
            lines.append(text["no_stage_data"])
            continue
        lines.extend(_render_fact_table(selected, settings=settings))

    lines.extend(["", f"## {sections['analysis']}", ""])
    if analysis is None:
        lines.extend([text["llm_failure"], "", text["no_findings"]])
    else:
        lines.append(
            f"- {fields['overall_assessment']}："
            f"{settings.definition_label('overall_assessments', analysis.overall_assessment)}"
        )
        for index, finding in enumerate(analysis.findings, start=1):
            lines.extend(
                [
                    "",
                    f"### {index}. {settings.definition_label('problem_categories', finding.category)}",
                    "",
                    f"- {fields['severity']}：{settings.definition_label('severities', finding.severity)}",
                    f"- {fields['evidence']}：{', '.join(finding.fact_ids)}",
                    f"- {fields['possible_causes']}：{_join_definition_labels(settings, 'possible_causes', finding.cause_ids)}",
                    f"- {fields['user_checks']}：{_join_definition_labels(settings, 'user_checks', finding.user_check_ids)}",
                ]
            )

    lines.extend(["", f"## {sections['product_suggestions']}", ""])
    if analysis is None:
        lines.append(text["llm_failure"])
    else:
        suggestion_ids = list(
            dict.fromkeys(
                suggestion_id
                for finding in analysis.findings
                for suggestion_id in finding.product_suggestion_ids
            )
        )
        lines.extend(
            f"- {settings.definition_label('product_suggestions', suggestion_id)}"
            for suggestion_id in suggestion_ids
        )

    lines.extend(["", f"## {sections['diagnostic_facts']}", ""])
    lines.extend(_render_fact_table(facts, settings=settings))
    return "\n".join(lines).rstrip() + "\n"


def scan_support_report_privacy(
    report_text: str,
    *,
    settings: SupportReportSettings,
) -> tuple[str, ...]:
    return tuple(
        key
        for key, pattern in settings.privacy_patterns
        if pattern.search(report_text)
    )


def collect_environment_versions(
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, str]:
    return {
        "worktrace_version": _safe_version(__version__),
        "python_version": _safe_version(platform.python_version()),
        "codex_version": _command_version(
            ("codex", "--version"), command_runner=command_runner
        ),
        "lark_cli_version": _command_version(
            ("lark-cli", "--version"), command_runner=command_runner
        ),
        "system_type": (
            platform.system()
            if platform.system() in {"Darwin", "Linux", "Windows"}
            else "unknown"
        ),
    }


def _render_fact_table(
    facts: Sequence[DiagnosticFact],
    *,
    settings: SupportReportSettings,
) -> list[str]:
    fields = settings.field_labels
    lines = [
        f"| {fields['fact_id']} | {fields['fact_type']} | {fields['metrics']} |",
        "| --- | --- | --- |",
    ]
    for fact in facts:
        metrics = "<br>".join(
            f"{settings.metric_labels.get(key, settings.safe_text['unknown'])}="
            f"{_format_metric_value(value, settings=settings)}"
            for key, value in fact.metrics.items()
        )
        lines.append(
            f"| {fact.fact_id} | "
            f"{settings.fact_type_labels.get(fact.kind, settings.safe_text['unknown'])} | "
            f"{metrics} |"
        )
    return lines


def _format_metric_value(
    value: int | float | str,
    *,
    settings: SupportReportSettings,
) -> str:
    if isinstance(value, str):
        return _escape_markdown(
            settings.value_labels.get(value, settings.safe_text["unknown"])
        )
    return str(value)


def _join_definition_labels(
    settings: SupportReportSettings,
    definition_name: str,
    ids: Iterable[str],
) -> str:
    return "；".join(
        settings.definition_label(definition_name, item_id) for item_id in ids
    )


def _write_support_report(text: str, *, config: RuntimeConfig, cwd: Path) -> Path:
    report_root = _absolute_path(
        config.data_root / "debug" / "support_reports",
        cwd=cwd,
    )
    report_root.mkdir(parents=True, exist_ok=True)
    for _attempt in range(5):
        report_path = report_root / f"worktrace-support-{secrets.token_hex(4)}.md"
        try:
            with report_path.open("x", encoding="utf-8") as handle:
                handle.write(text)
            return report_path
        except FileExistsError:
            continue
        except OSError:
            report_path.unlink(missing_ok=True)
            raise
    raise OSError("Unable to allocate a support report path.")


def _display_path(path: Path, *, cwd: Path) -> str:
    try:
        return str(path.relative_to(cwd))
    except ValueError:
        return str(path)


def _output_exists(output_path: str | None, *, cwd: Path) -> bool:
    if not output_path:
        return False
    path = Path(output_path)
    if not path.is_absolute():
        path = cwd / path
    return path.is_file()


def _absolute_path(path: Path, *, cwd: Path) -> Path:
    return path if path.is_absolute() else cwd / path


def _read_debug_json(path: Path) -> dict[str, object]:
    try:
        if path.stat().st_size > _MAX_DEBUG_JSON_BYTES:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _failed_backend_count(summary: Mapping[str, object]) -> int:
    by_backend = summary.get("by_backend")
    if not isinstance(by_backend, Mapping):
        return 0
    return sum(
        _mapping_count(item, "failed_count")
        for item in by_backend.values()
        if isinstance(item, Mapping)
    )


def _mapping_count(mapping: Mapping[str, object], key: str) -> int:
    return _safe_count(mapping.get(key, 0))


def _safe_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


def _safe_nonnegative_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(max(float(value), 0.0), 3)


def _normalize_delivery_status(
    value: str,
    *,
    settings: SupportReportSettings,
) -> str:
    if value in {"success", "failed"}:
        return value
    if not value:
        return "not_attempted"
    return _known_value(value, settings=settings)


def _known_value(value: str, *, settings: SupportReportSettings) -> str:
    return value if value in settings.value_labels else "unknown"


def _safe_version(value: str) -> str:
    match = _VERSION_PATTERN.search(value)
    return match.group(0) if match else "unknown"


def _command_version(
    command: tuple[str, ...],
    *,
    command_runner: Callable[..., subprocess.CompletedProcess[str]],
) -> str:
    try:
        result = command_runner(
            command,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    combined = f"{getattr(result, 'stdout', '')}\n{getattr(result, 'stderr', '')}"
    return _safe_version(combined)


def _validated_id_list(value: object, allowed: set[str]) -> list[str] | None:
    if not isinstance(value, list) or not value:
        return None
    if any(not isinstance(item, str) or item not in allowed for item in value):
        return None
    if len(set(value)) != len(value):
        return None
    return list(value)


def _enum_schema(values: Sequence[str]) -> dict[str, object]:
    return {"type": "string", "enum": list(values)}


def _array_enum_schema(values: Sequence[str]) -> dict[str, object]:
    return {
        "type": "array",
        "items": _enum_schema(values),
        "minItems": 1,
        "uniqueItems": True,
    }


def _escape_markdown(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _required_dict(payload: Mapping[str, object], key: str) -> dict[str, object]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Support report config field {key} must be an object.")
    return value


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Support report config field {key} must be a string.")
    return value.strip()


def _required_int(payload: Mapping[str, object], key: str, *, minimum: int) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"Support report config field {key} must be an integer.")
    return value


def _required_number(
    payload: Mapping[str, object],
    key: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Support report config field {key} must be a number.")
    normalized = float(value)
    if not minimum <= normalized <= maximum:
        raise ValueError(f"Support report config field {key} is out of range.")
    return normalized


def _string_list(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"Support report config field {key} must be a list.")
    normalized = tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    if len(normalized) != len(value):
        raise ValueError(f"Support report config field {key} contains invalid text.")
    return normalized


def _string_dict(payload: Mapping[str, object], key: str) -> dict[str, str]:
    value = _required_dict(payload, key)
    normalized = {
        item_key: item_value.strip()
        for item_key, item_value in value.items()
        if isinstance(item_key, str)
        and isinstance(item_value, str)
        and item_key
        and item_value.strip()
    }
    if len(normalized) != len(value):
        raise ValueError(f"Support report config field {key} contains invalid text.")
    return normalized


def _definition_list(
    payload: Mapping[str, object], key: str
) -> tuple[dict[str, str], ...]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"Support report definition {key} must be a list.")
    definitions: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"key", "label"}:
            raise ValueError(f"Support report definition {key} is invalid.")
        item_key = item.get("key")
        label = item.get("label")
        if (
            not isinstance(item_key, str)
            or not _SAFE_KEY_PATTERN.fullmatch(item_key)
            or item_key in seen
            or not isinstance(label, str)
            or not label.strip()
        ):
            raise ValueError(f"Support report definition {key} is invalid.")
        seen.add(item_key)
        definitions.append({"key": item_key, "label": label.strip()})
    return tuple(definitions)


def _error_category_list(payload: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    value = payload.get("error_categories")
    if not isinstance(value, list) or not value:
        raise ValueError("Support report error categories must be a list.")
    categories: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"key", "patterns"}:
            raise ValueError("Support report error category is invalid.")
        item_key = item.get("key")
        patterns = item.get("patterns")
        if not isinstance(item_key, str) or not _SAFE_KEY_PATTERN.fullmatch(item_key):
            raise ValueError("Support report error category key is invalid.")
        if not isinstance(patterns, list) or not patterns or not all(
            isinstance(pattern, str) and pattern for pattern in patterns
        ):
            raise ValueError("Support report error category patterns are invalid.")
        for pattern in patterns:
            re.compile(pattern)
        categories.append({"key": item_key, "patterns": tuple(patterns)})
    return tuple(categories)


def _privacy_pattern_list(
    payload: Mapping[str, object],
) -> tuple[tuple[str, re.Pattern[str]], ...]:
    value = payload.get("privacy_patterns")
    if not isinstance(value, list) or not value:
        raise ValueError("Support report privacy patterns must be a list.")
    patterns: list[tuple[str, re.Pattern[str]]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"key", "pattern"}:
            raise ValueError("Support report privacy pattern is invalid.")
        item_key = item.get("key")
        pattern = item.get("pattern")
        if not isinstance(item_key, str) or not isinstance(pattern, str):
            raise ValueError("Support report privacy pattern is invalid.")
        patterns.append((item_key, re.compile(pattern, flags=re.IGNORECASE)))
    return tuple(patterns)
