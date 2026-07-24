from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.worktrace.analyzers.function_calls import collected_grouping_call_contract
from src.worktrace.analyzers.collected_evidence import (
    EvidenceRelation,
    derive_group_evidence,
    derive_semantic_review_trigger_reasons,
)
from src.worktrace.analyzers.prompts import (
    build_collected_grouping_prompt,
    build_collected_review_prompt,
)
from src.worktrace.analyzers.protocol import (
    parse_collected_grouping_function_payload,
    parse_collected_grouping_payload,
)
from src.worktrace.collected_merge import (
    _collected_review_result_basis_error,
    collected_grouping_partition_error,
    collected_grouping_validation_feedback,
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
from src.worktrace.utils.token_estimation import estimate_structured_input_tokens

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
        "--trace-root",
        type=Path,
        help="可选；直接读取多人合并 trace 目录，离线回放候选分组和高风险复核。",
    )
    parser.add_argument(
        "--steps",
        help="配合 --trace-root 使用；逗号分隔的 step 编号，如 13,14,17。",
    )
    parser.add_argument(
        "--result-dir",
        type=Path,
        help="可选；优先读取此目录下的 M07.json 或 step-013.json 等新实验结果。",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="可选；写入最新 prompt、Function 定义、summary.json 和 summary.md。",
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
    split_reason_source = (
        "top_level"
        if result.split_reason.strip()
        else "legacy_group"
        if any(group.split_reason.strip() for group in result.groups)
        else ""
    )
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
        "split_reason_source": split_reason_source,
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


def evaluate_split_reason_compatibility(
    *,
    result: CollectedGroupingResult,
    source_events: list[CollectedSourceEvent],
    candidate_group: CollectedGroupingGroup,
    review_reasons: list[str],
    config: RuntimeConfig,
) -> dict[str, Any]:
    split_reason = effective_split_reason(result)
    if not split_reason or len(result.groups) <= 1:
        return {"tested": False, "reason": "result_was_not_split_with_a_reason"}

    groups_without_reasons = [
        replace(group, split_reason="") for group in result.groups
    ]
    top_level = evaluate_review_result(
        result=CollectedGroupingResult(
            groups=groups_without_reasons,
            split_reason=split_reason,
            validation_errors=list(result.validation_errors),
        ),
        source_events=source_events,
        candidate_group=candidate_group,
        review_reasons=review_reasons,
        config=config,
    )
    legacy_groups = list(groups_without_reasons)
    legacy_groups[0] = replace(legacy_groups[0], split_reason=split_reason)
    legacy_group = evaluate_review_result(
        result=CollectedGroupingResult(
            groups=legacy_groups,
            split_reason="",
            validation_errors=list(result.validation_errors),
        ),
        source_events=source_events,
        candidate_group=candidate_group,
        review_reasons=review_reasons,
        config=config,
    )

    return {
        "tested": True,
        "top_level": {
            "accepted": "missing_overall_split_reason" not in top_level["errors"],
            "source": top_level["split_reason_source"],
        },
        "legacy_group": {
            "accepted": "missing_overall_split_reason" not in legacy_group["errors"],
            "source": legacy_group["split_reason_source"],
        },
    }


def _split_validation_errors(value: object) -> list[str]:
    if isinstance(value, list):
        raw_errors = [str(item).strip() for item in value]
    elif isinstance(value, str):
        raw_errors = [item.strip() for item in value.split(";")]
    else:
        raw_errors = []
    return list(dict.fromkeys(item for item in raw_errors if item))


def _error_code(error: str) -> str:
    return error.strip().split(maxsplit=1)[0]


def _issue_counts(errors: list[str]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for error in errors:
        code = _error_code(error)
        if code in {"duplicate_draft_id", "duplicate_group_member"}:
            counts["duplicate_draft_id"] += 1
        if code == "merged_group_too_small":
            counts["single_member_merge"] += 1
        if code == "merge_reason_missing":
            counts["merge_reason_missing"] += 1
        if code == "reason_detail_missing":
            counts["reason_detail_missing"] += 1
        if code == "evidence_outside_group":
            counts["evidence_outside_group"] += 1
        if code == "evidence_does_not_cover_group":
            counts["evidence_does_not_cover_group"] += 1
        if code == "groups_without_merge_basis":
            counts["evidence_does_not_cover_group"] += 1
        if code in {
            "member_connections_missing",
            "invalid_member_connection",
            "member_connection_detail_missing",
            "duplicate_member_connection",
            "unknown_member_connection",
            "missing_member_connection",
        }:
            counts["member_connection_error"] += 1
    return dict(counts)


def _stage_name(stage: str) -> str:
    return "candidate_grouping" if stage.startswith("candidate_") else stage


def _source_target_date(source_events: list[CollectedSourceEvent]) -> str:
    return next(
        (
            item.event.date
            for item in source_events
            if str(item.event.date).strip()
        ),
        "",
    )


def _load_source_events(trace: dict[str, Any]) -> list[CollectedSourceEvent]:
    return [
        source_event_from_trace(payload)
        for payload in trace.get("input_events", [])
        if isinstance(payload, dict)
    ]


def _candidate_group_for_trace(
    trace: dict[str, Any],
    source_events: list[CollectedSourceEvent],
) -> CollectedGroupingGroup:
    candidate_payload = trace.get("candidate_group")
    if isinstance(candidate_payload, dict):
        return CollectedGroupingGroup.from_dict(candidate_payload)
    return CollectedGroupingGroup(
        group_id=f"step-{int(trace.get('step_index', 0) or 0):03d}",
        draft_ids=[item.draft_id for item in source_events],
    )


def _resolve_override_path(result_dir: Path | None, case_id: str) -> Path | None:
    if result_dir is None:
        return None
    numeric_id = int(case_id) if case_id.isdigit() else None
    names = [f"{case_id}.json"]
    if numeric_id is not None:
        names = [f"step-{numeric_id:03d}.json", f"{numeric_id:03d}.json", *names]
    for name in names:
        candidate = result_dir / name
        if candidate.exists():
            return candidate
    raise FileNotFoundError(result_dir / names[0])


def _model_declared_evidence_relation_ids(
    raw_payload: dict[str, Any],
) -> list[dict[str, object]]:
    raw_groups = raw_payload.get("groups", raw_payload.get("merged_groups", []))
    if not isinstance(raw_groups, list):
        return []
    return [
        {
            "group_id": str(group.get("group_id", "")),
            "relation_ids": [
                str(value) for value in group.get("evidence_relation_ids", [])
            ],
        }
        for group in raw_groups
        if isinstance(group, dict) and group.get("evidence_relation_ids")
    ]


def _audit_groups(
    result: CollectedGroupingResult,
    source_events: list[CollectedSourceEvent],
    relations: list[EvidenceRelation],
    config: RuntimeConfig,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[str]]:
    source_by_id = {item.draft_id: item for item in source_events}
    evidence_audits: list[dict[str, object]] = []
    semantic_audits: list[dict[str, object]] = []
    review_triggers: list[str] = []
    for group in result.groups:
        if len(group.draft_ids) < 2:
            continue
        evidence_audit = derive_group_evidence(group.draft_ids, relations)
        evidence_audits.append(
            {"group_id": group.group_id, **evidence_audit.to_dict()}
        )
        group_sources = [
            source_by_id[draft_id]
            for draft_id in group.draft_ids
            if draft_id in source_by_id
        ]
        group_triggers = list(
            derive_semantic_review_trigger_reasons(
                group=group,
                source_events=group_sources,
                config=config,
                relations=relations,
            )
        )
        review_triggers.extend(group_triggers)
        connection_ids = [item.draft_id for item in group.member_connections]
        semantic_audits.append(
            {
                "group_id": group.group_id,
                "semantic_reasons": list(group.semantic_reasons),
                "reason_detail": group.reason_detail,
                "member_connection_draft_ids": connection_ids,
                "missing_member_connection_draft_ids": sorted(
                    set(group.draft_ids).difference(connection_ids)
                ),
                "review_trigger_reasons": group_triggers,
            }
        )
    return evidence_audits, semantic_audits, review_triggers


def _legacy_new_rule_errors(
    result: CollectedGroupingResult,
    expected_draft_ids: list[str],
    relations: list[EvidenceRelation],
    config: RuntimeConfig,
) -> list[str]:
    structural_result = CollectedGroupingResult(
        groups=list(result.groups),
        split_reason=result.split_reason,
    )
    errors = _split_validation_errors(
        collected_grouping_partition_error(structural_result, expected_draft_ids)
    )
    semantic_reason_keys = {
        item.key
        for item in config.collected_group_reason_definitions
        if item.supports_semantic_merge
    }
    for group in result.groups:
        if len(group.draft_ids) < 2:
            if not group.group_id.startswith("singleton-"):
                errors.append(
                    "merged_group_too_small "
                    f"group_id={group.group_id} draft_ids={group.draft_ids}"
                )
            continue
        audit = derive_group_evidence(group.draft_ids, relations)
        semantic_reasons = semantic_reason_keys.intersection(
            [*group.semantic_reasons, *group.group_reason]
        )
        if not semantic_reasons and audit.contained_relation_ids and not audit.connected:
            errors.append(
                "evidence_does_not_cover_group "
                f"group_id={group.group_id} "
                f"relation_ids={list(audit.contained_relation_ids)} "
                f"uncovered_draft_ids={list(audit.uncovered_draft_ids)}"
            )
        elif not semantic_reasons and not audit.connected:
            errors.append(f"merge_reason_missing group_id={group.group_id}")
        if not group.reason_detail.strip():
            errors.append(f"reason_detail_missing group_id={group.group_id}")
    return list(dict.fromkeys(errors))


def _new_rule_handling(mode: str, errors: list[str], has_multi_group: bool) -> str:
    if mode == "current":
        return (
            "完整执行协议 v2 校验；错误进入当前请求重试。"
            if errors
            else "协议 v2 校验通过，证据编号由 Python 自动计算。"
        )
    codes = {_error_code(error) for error in errors}
    handling: list[str] = []
    if codes.intersection({"duplicate_draft_id", "duplicate_group_member"}):
        handling.append("拒绝重复事件编号并反馈具体编号")
    if "merged_group_too_small" in codes:
        handling.append("拒绝单成员合并组，要求改放单条列表")
    if "evidence_outside_group" in codes:
        handling.append("忽略模型声明的越界证据，由 Python 重新选组内证据")
    if "evidence_does_not_cover_group" in codes:
        handling.append("局部证据只写审计，不能作为全组合并依据")
    if codes.intersection({"merge_reason_missing", "reason_detail_missing"}):
        handling.append("缺少合法理由或理由详情时拒绝该分组")
    if has_multi_group:
        handling.append("旧记录不补造逐事件说明，重新调用模型时必须完整返回")
    return "；".join(handling) or "按新规则重新计算组内证据"


def replay_trace_payload(
    *,
    case_id: str,
    trace_path: Path,
    target_date: str,
    failure_types: list[str],
    config: RuntimeConfig,
    result_dir: Path | None,
) -> tuple[dict[str, Any], str, dict[str, object]]:
    trace = read_json(trace_path)
    source_events = _load_source_events(trace)
    if not source_events:
        raise ValueError(f"{case_id} trace has no input_events.")
    stage = str(trace.get("stage", ""))
    if not (stage.startswith("candidate_") or stage == "high_risk_review"):
        raise ValueError(f"{case_id} is not a collected grouping step: stage={stage}.")
    candidate_group = _candidate_group_for_trace(trace, source_events)
    review_reasons = [str(value) for value in trace.get("review_reasons", [])]
    deterministic_groups = [
        [str(draft_id) for draft_id in group]
        for group in trace.get("deterministic_groups", [])
        if isinstance(group, list)
    ]
    is_review = stage == "high_risk_review"
    request_kind = (
        "collected_group_review" if is_review else "collected_candidate_grouping"
    )
    contract = collected_grouping_call_contract(
        request_kind,
        config=config,
        events=source_events,
        deterministic_groups=(
            [list(candidate_group.draft_ids)] if is_review else deterministic_groups
        ),
        include_split_reason=is_review,
    )
    prompt = (
        build_collected_review_prompt(
            target_date or _source_target_date(source_events),
            source_events,
            candidate_group,
            config=config,
            review_reasons=review_reasons,
        )
        if is_review
        else build_collected_grouping_prompt(
            target_date or _source_target_date(source_events),
            source_events,
            deterministic_groups,
            config=config,
        )
    )
    override_path = _resolve_override_path(result_dir, case_id)
    raw_payload = load_result_payload(trace, override_path)
    if not isinstance(raw_payload, dict):
        raise ValueError(f"{case_id} result is not an object.")
    is_function_payload = (
        "merged_groups" in raw_payload or "singleton_draft_ids" in raw_payload
    )
    mode = (
        "current"
        if override_path is not None
        or int(trace.get("grouping_protocol_version", 0) or 0) >= 2
        else "legacy_audit"
    )
    if mode == "current" and is_function_payload:
        result, _function_errors = parse_collected_grouping_function_payload(
            raw_payload,
            evidence_catalog=list(contract.evidence_catalog),
            allowed_semantic_reasons=contract.semantic_reasons,
        )
    else:
        result = parse_collected_grouping_payload(raw_payload)

    expected_draft_ids = (
        list(candidate_group.draft_ids)
        if is_review
        else [item.draft_id for item in source_events]
    )
    original_errors = _split_validation_errors(
        trace.get("python_validation", {}).get("errors", [])
        if isinstance(trace.get("python_validation"), dict)
        else []
    )
    if mode == "legacy_audit":
        validation_errors = _legacy_new_rule_errors(
            result,
            expected_draft_ids,
            list(contract.evidence_catalog),
            config,
        )
    else:
        partition_error = collected_grouping_partition_error(result, expected_draft_ids)
        relation_error = (
            _collected_review_result_basis_error(
                result,
                source_events,
                reasons=review_reasons,
                config=config,
            )
            if is_review
            else ""
        )
        split_reason_error = (
            "missing_overall_split_reason field=split_reason "
            f"group_id={candidate_group.group_id}"
            if is_review
            and len(candidate_group.draft_ids) > 1
            and len(result.groups) > 1
            and not effective_split_reason(result)
            else ""
        )
        validation_errors = list(
            dict.fromkeys(
                error
                for value in (partition_error, relation_error, split_reason_error)
                for error in _split_validation_errors(value)
            )
        )
    evidence_audits, semantic_audits, review_triggers = _audit_groups(
        result,
        source_events,
        list(contract.evidence_catalog),
        config,
    )
    has_multi_group = any(len(group.draft_ids) > 1 for group in result.groups)
    needs_model_review = bool(
        validation_errors
        or review_triggers
        or (mode == "legacy_audit" and has_multi_group)
    )
    estimates = estimate_structured_input_tokens(
        prompt,
        function_spec=contract.function_spec,
        append_no_think=True,
    )
    counted_errors = (
        list(
            dict.fromkeys(
                [
                    *original_errors,
                    *(
                        error
                        for error in validation_errors
                        if _error_code(error)
                        in {
                            "merged_group_too_small",
                            "evidence_does_not_cover_group",
                        }
                    ),
                ]
            )
        )
        if mode == "legacy_audit"
        else validation_errors
    )
    issue_counts = _issue_counts(counted_errors)
    if mode == "legacy_audit":
        legacy_multi_group_count = sum(
            len(group.draft_ids) > 1 for group in result.groups
        )
        if legacy_multi_group_count:
            issue_counts["member_connection_error"] = (
                issue_counts.get("member_connection_error", 0)
                + legacy_multi_group_count
            )
    evaluation: dict[str, Any] = {
        "id": case_id,
        "step_index": int(trace.get("step_index", 0) or 0),
        "stage": stage,
        "stage_group": _stage_name(stage),
        "mode": mode,
        "valid": not validation_errors,
        "errors": validation_errors,
        "original_errors": original_errors,
        "failure_types": failure_types,
        "group_count": len(result.groups),
        "split_reason": effective_split_reason(result),
        "split_reason_source": (
            "top_level"
            if result.split_reason.strip()
            else "legacy_group"
            if any(group.split_reason.strip() for group in result.groups)
            else ""
        ),
        "source_trace": str(trace_path.relative_to(REPO_ROOT))
        if trace_path.is_relative_to(REPO_ROOT)
        else str(trace_path),
        "result_source": (
            str(override_path.relative_to(REPO_ROOT))
            if override_path is not None and override_path.is_relative_to(REPO_ROOT)
            else str(override_path)
            if override_path is not None
            else "recorded_raw_result"
        ),
        "function_name": contract.function_spec.name,
        "evidence_relation_catalog": [
            relation.to_dict() for relation in contract.evidence_catalog
        ],
        "evidence_audit": evidence_audits,
        "semantic_audit": semantic_audits,
        "model_declared_evidence_relation_ids": (
            _model_declared_evidence_relation_ids(raw_payload)
            if mode == "legacy_audit"
            else []
        ),
        "python_validation": {
            "valid": not validation_errors,
            "errors": validation_errors,
            "retry_feedback": (
                collected_grouping_validation_feedback("; ".join(validation_errors))
                if validation_errors
                else ""
            ),
        },
        "issue_counts": issue_counts,
        "review_trigger_reasons": review_triggers,
        "new_review_trigger_count": len(review_triggers),
        "new_rule_handling": _new_rule_handling(
            mode,
            [*original_errors, *validation_errors],
            has_multi_group,
        ),
        "needs_model_review": needs_model_review,
        "input_estimates": estimates,
        "input_target_tokens": config.model_input_batch_target_tokens,
        "input_overage_reason": (
            "minimum_required_input"
            if estimates["input_estimated_tokens"]
            > config.model_input_batch_target_tokens
            else ""
        ),
    }
    if is_review:
        evaluation["split_reason_compatibility"] = (
            evaluate_split_reason_compatibility(
                result=result,
                source_events=source_events,
                candidate_group=candidate_group,
                review_reasons=review_reasons,
                config=config,
            )
            if mode == "legacy_audit"
            else {"tested": False, "reason": "current_protocol"}
        )
    return (
        evaluation,
        contract.function_spec.prompt_with_example(prompt),
        contract.function_spec.tool(),
    )


def replay_case(
    item: dict[str, Any],
    *,
    config: RuntimeConfig,
    result_dir: Path | None = None,
) -> tuple[dict[str, Any], str, dict[str, object]]:
    case_id = str(item["id"])
    original_prompt_path = resolve_repo_path(Path(str(item["prompt_path"])))
    return replay_trace_payload(
        case_id=case_id,
        trace_path=trace_path_from_prompt(original_prompt_path),
        target_date=str(item.get("target_date", "")),
        failure_types=[str(value) for value in item.get("failure_types", [])],
        config=config,
        result_dir=result_dir,
    )


def load_trace_cases(trace_root: Path, step_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    if not step_ids:
        paths = sorted(trace_root.glob("step-*.json"))
    else:
        paths = []
        for value in step_ids:
            try:
                step_index = int(value)
            except ValueError as exc:
                raise ValueError(f"Invalid step number: {value}") from exc
            path = trace_root / f"step-{step_index:03d}.json"
            if not path.exists():
                raise FileNotFoundError(path)
            paths.append(path)
    cases: list[dict[str, Any]] = []
    for path in paths:
        payload = read_json(path)
        if not isinstance(payload, dict):
            continue
        stage = str(payload.get("stage", ""))
        if not (stage.startswith("candidate_") or stage == "high_risk_review"):
            if step_ids:
                raise ValueError(f"{path.name} is not a grouping step: stage={stage}.")
            continue
        cases.append(
            {
                "id": str(int(path.stem.removeprefix("step-"))),
                "trace_path": path,
            }
        )
    return cases


def build_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    issue_counts: Counter[str] = Counter()
    review_trigger_counts: Counter[str] = Counter()
    stages: dict[str, dict[str, Any]] = {}
    for item in results:
        issue_counts.update(item.get("issue_counts", {}))
        review_trigger_counts.update(item.get("review_trigger_reasons", []))
        stage = str(item.get("stage_group", "unknown"))
        stage_summary = stages.setdefault(
            stage,
            {
                "case_count": 0,
                "issue_counts": Counter(),
                "review_trigger_counts": Counter(),
            },
        )
        stage_summary["case_count"] += 1
        stage_summary["issue_counts"].update(item.get("issue_counts", {}))
        stage_summary["review_trigger_counts"].update(
            item.get("review_trigger_reasons", [])
        )
    serialized_stages = {
        stage: {
            "case_count": values["case_count"],
            "issue_counts": dict(values["issue_counts"]),
            "review_trigger_counts": dict(values["review_trigger_counts"]),
        }
        for stage, values in stages.items()
    }
    return {
        "case_count": len(results),
        "valid_count": sum(bool(item["valid"]) for item in results),
        "invalid_count": sum(not bool(item["valid"]) for item in results),
        "issue_counts": dict(issue_counts),
        "review_trigger_counts": dict(review_trigger_counts),
        "by_stage": serialized_stages,
        "model_call_count": 0,
        "results": results,
    }


def _markdown_problem_text(item: dict[str, Any]) -> str:
    errors = item.get("original_errors", [])
    if not errors and item.get("mode") == "current":
        errors = item.get("errors", [])
    return "<br>".join(str(value).replace("|", "\\|") for value in errors) or "-"


def build_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# 多人汇总分组离线回放",
        "",
        f"- 素材数：{summary['case_count']}",
        f"- 当前规则通过：{summary['valid_count']}",
        f"- 当前规则不通过：{summary['invalid_count']}",
        f"- 模型调用：{summary['model_call_count']}",
        f"- 问题统计：`{json.dumps(summary['issue_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- 新增复核触发：`{json.dumps(summary['review_trigger_counts'], ensure_ascii=False, sort_keys=True)}`",
        "",
        "| 编号 | 阶段 | 模式 | 旧结果问题 | 新规则处理 | 是否仍需模型复核 |",
        "|---|---|---|---|---|---|",
    ]
    for item in summary["results"]:
        lines.append(
            f"| {item['id']} | {item['stage']} | {item['mode']} | "
            f"{_markdown_problem_text(item)} | "
            f"{str(item['new_rule_handling']).replace('|', '\\|')} | "
            f"{'是' if item['needs_model_review'] else '否'} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    result_dir = resolve_repo_path(args.result_dir) if args.result_dir else None
    output_dir = resolve_repo_path(args.output_dir) if args.output_dir else None
    config = load_runtime_config()
    results: list[dict[str, Any]] = []
    prompts: dict[str, str] = {}
    functions: dict[str, dict[str, object]] = {}
    if args.trace_root is not None:
        trace_root = resolve_repo_path(args.trace_root)
        step_ids = tuple(
            value.strip()
            for value in str(args.steps or "").split(",")
            if value.strip()
        )
        cases = load_trace_cases(trace_root, step_ids)
        replay_inputs = [
            {
                "case_id": item["id"],
                "trace_path": item["trace_path"],
                "target_date": "",
                "failure_types": [],
            }
            for item in cases
        ]
    else:
        if args.steps:
            raise ValueError("--steps requires --trace-root.")
        inventory_path = resolve_repo_path(args.inventory)
        case_ids = tuple(
            value.strip() for value in str(args.ids).split(",") if value.strip()
        )
        replay_inputs = [
            {
                "inventory_item": item,
                "case_id": str(item["id"]),
            }
            for item in load_inventory_cases(inventory_path, case_ids)
        ]

    for replay_input in replay_inputs:
        if "inventory_item" in replay_input:
            evaluation, prompt, function_definition = replay_case(
                replay_input["inventory_item"],
                config=config,
                result_dir=result_dir,
            )
        else:
            evaluation, prompt, function_definition = replay_trace_payload(
                case_id=str(replay_input["case_id"]),
                trace_path=replay_input["trace_path"],
                target_date=str(replay_input["target_date"]),
                failure_types=list(replay_input["failure_types"]),
                config=config,
                result_dir=result_dir,
            )
        results.append(evaluation)
        output_id = (
            f"step-{int(evaluation['id']):03d}"
            if str(evaluation["id"]).isdigit()
            else str(evaluation["id"])
        )
        prompts[output_id] = prompt
        functions[output_id] = function_definition
    summary = build_summary(results)

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
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
            write_json(output_dir / f"{case_id}-function.json", functions[case_id])

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for item in results:
            errors = "; ".join(item["errors"]) or "none"
            print(
                f"{item['id']} stage={item['stage']} mode={item['mode']} "
                f"valid={str(item['valid']).lower()} "
                f"groups={item['group_count']} "
                f"input_estimated_tokens={item['input_estimates']['input_estimated_tokens']} "
                f"needs_model_review={str(item['needs_model_review']).lower()} "
                f"errors={errors}"
            )
        print(
            f"SUMMARY cases={summary['case_count']} valid={summary['valid_count']} "
            f"invalid={summary['invalid_count']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
