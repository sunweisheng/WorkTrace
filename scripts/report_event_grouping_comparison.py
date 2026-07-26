from __future__ import annotations

import argparse
import json
from collections import Counter
from itertools import combinations
from pathlib import Path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare personal day-event grouping structures without semantic judgment."
    )
    parser.add_argument("--date", required=True, help="Target date in YYYY-MM-DD format.")
    parser.add_argument("--baseline-trace-root", required=True)
    parser.add_argument("--current-trace-root", required=True)
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-markdown", default=None)
    return parser.parse_args(argv)


def _read_object(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected an object in {path}")
    return payload


def _merge_root(trace_root: Path, target_date: str) -> Path:
    return (
        trace_root
        / "conversation_debug"
        / target_date
        / "_merge_day_candidates"
    )


def _normalized_groups(payload: dict[str, object]) -> list[dict[str, object]]:
    raw_groups = payload.get("groups", [])
    groups = raw_groups if isinstance(raw_groups, list) else []
    normalized: list[dict[str, object]] = []
    for index, item in enumerate(groups, start=1):
        if not isinstance(item, dict):
            continue
        draft_ids = item.get("draft_ids", [])
        evidence_ids = item.get("evidence_message_ids", [])
        normalized.append(
            {
                "group_id": str(item.get("group_id", f"group-{index:03d}")),
                "draft_ids": (
                    [str(value) for value in draft_ids]
                    if isinstance(draft_ids, list)
                    else []
                ),
                "primary_draft_id": str(item.get("primary_draft_id", "")),
                "merge_reason": str(item.get("merge_reason", "")),
                "evidence_message_ids": (
                    [str(value) for value in evidence_ids]
                    if isinstance(evidence_ids, list)
                    else []
                ),
            }
        )
    return normalized


def _coverage(
    expected_ids: list[str],
    groups: list[dict[str, object]],
) -> dict[str, object]:
    expected = set(expected_ids)
    returned = [
        str(draft_id)
        for group in groups
        for draft_id in group.get("draft_ids", [])
    ]
    counts = Counter(returned)
    missing = [draft_id for draft_id in expected_ids if counts[draft_id] == 0]
    duplicate = [draft_id for draft_id in expected_ids if counts[draft_id] > 1]
    unknown = list(dict.fromkeys(item for item in returned if item not in expected))
    return {
        "expected_candidate_count": len(expected_ids),
        "returned_candidate_count": len(returned),
        "unique_returned_candidate_count": len(set(returned)),
        "missing_draft_ids": missing,
        "duplicate_draft_ids": duplicate,
        "unknown_draft_ids": unknown,
        "valid": not missing and not duplicate and not unknown,
    }


def _group_pairs(groups: list[dict[str, object]]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for group in groups:
        draft_ids = [str(item) for item in group.get("draft_ids", [])]
        pairs.update(tuple(sorted(pair)) for pair in combinations(draft_ids, 2))
    return pairs


def _membership(groups: list[dict[str, object]]) -> dict[str, list[str]]:
    membership: dict[str, list[str]] = {}
    for group in groups:
        draft_ids = [str(item) for item in group.get("draft_ids", [])]
        for draft_id in draft_ids:
            membership[draft_id] = list(draft_ids)
    return membership


def _review_summary(path: Path) -> dict[str, object]:
    if not path.exists():
        return {
            "exists": False,
            "component_count": 0,
            "request_count": 0,
            "components": [],
        }
    payload = _read_object(path)
    raw_attempts = payload.get("attempts", [])
    attempts = (
        [item for item in raw_attempts if isinstance(item, dict)]
        if isinstance(raw_attempts, list)
        else []
    )
    by_component: dict[str, list[dict[str, object]]] = {}
    for attempt in attempts:
        component_id = str(attempt.get("component_id", "unknown"))
        by_component.setdefault(component_id, []).append(attempt)
    raw_component_records = payload.get("components", [])
    component_records = (
        [item for item in raw_component_records if isinstance(item, dict)]
        if isinstance(raw_component_records, list)
        else []
    )
    component_record_by_id = {
        str(item.get("component_id", "")): item
        for item in component_records
        if str(item.get("component_id", ""))
    }
    component_ids = list(
        dict.fromkeys([*component_record_by_id, *by_component])
    )
    components: list[dict[str, object]] = []
    for component_id in component_ids:
        items = by_component.get(component_id, [])
        last = items[-1] if items else {}
        component_record = component_record_by_id.get(component_id, {})
        input_payload = last.get("input", {})
        relation_reasons = (
            component_record.get("relation_reasons", [])
            or (
                input_payload.get("relation_reasons", [])
                if isinstance(input_payload, dict)
                else []
            )
        )
        raw_result = last.get("result", {})
        components.append(
            {
                "component_id": component_id,
                "group_ids": component_record.get("group_ids", []),
                "relation_sources": component_record.get(
                    "relation_sources", []
                ),
                "request_count": len(items),
                "final_status": str(last.get("status", "not_requested")),
                "final_backend": str(last.get("backend", "none")),
                "validation_error": str(last.get("validation_error", "")),
                "candidate_draft_ids": (
                    [str(value) for value in input_payload.get("candidate_draft_ids", [])]
                    if isinstance(input_payload, dict)
                    and isinstance(input_payload.get("candidate_draft_ids"), list)
                    else []
                ),
                "relation_reasons": relation_reasons
                if isinstance(relation_reasons, list)
                else [],
                "final_groups": (
                    raw_result.get("groups", [])
                    if isinstance(raw_result, dict)
                    and isinstance(raw_result.get("groups"), list)
                    else []
                ),
                "attempts": [
                    {
                        "attempt": item.get("attempt"),
                        "backend": item.get("backend"),
                        "status": item.get("status"),
                        "validation_error": item.get("validation_error", ""),
                    }
                    for item in items
                ],
            }
        )
    return {
        "exists": True,
        "component_count": len(components),
        "request_count": len(attempts),
        "components": components,
    }


def _discovery_summary(
    path: Path,
    review: dict[str, object],
) -> dict[str, object]:
    if not path.exists():
        return {
            "available": False,
            "status": "unavailable",
            "protocol": None,
            "checked_group_count": None,
            "candidate_group_count": None,
            "candidate_groups": [],
        }
    payload = _read_object(path)
    result = payload.get("result", {})
    raw_candidates = (
        result.get("candidate_groups", [])
        if isinstance(result, dict)
        else []
    )
    candidates = (
        [item for item in raw_candidates if isinstance(item, dict)]
        if isinstance(raw_candidates, list)
        else []
    )
    raw_checks = result.get("group_checks") if isinstance(result, dict) else None
    review_components = review.get("components", [])
    components = (
        [item for item in review_components if isinstance(item, dict)]
        if isinstance(review_components, list)
        else []
    )
    summarized_candidates: list[dict[str, object]] = []
    for candidate in candidates:
        group_ids = [
            str(item) for item in candidate.get("group_ids", [])
        ] if isinstance(candidate.get("group_ids"), list) else []
        matched_component: dict[str, object] | None = None
        for component in components:
            relation_reasons = component.get("relation_reasons", [])
            if not isinstance(relation_reasons, list):
                continue
            if any(
                isinstance(reason, dict)
                and "title_discovery" in reason.get("relation_types", [])
                and set(str(item) for item in reason.get("group_ids", []))
                == set(group_ids)
                for reason in relation_reasons
            ):
                matched_component = component
                break
        summarized_candidates.append(
            {
                "group_ids": group_ids,
                "reason": str(candidate.get("reason", "")),
                "review_component_id": (
                    matched_component.get("component_id")
                    if matched_component is not None
                    else None
                ),
                "review_final_status": (
                    matched_component.get("final_status")
                    if matched_component is not None
                    else "not_found"
                ),
                "review_final_backend": (
                    matched_component.get("final_backend")
                    if matched_component is not None
                    else "none"
                ),
                "reviewed_groups": (
                    matched_component.get("final_groups", [])
                    if matched_component is not None
                    else []
                ),
            }
        )
    return {
        "available": True,
        "status": str(payload.get("status", "unknown")),
        "protocol": payload.get("protocol"),
        "checked_group_count": len(raw_checks) if isinstance(raw_checks, list) else None,
        "candidate_group_count": len(summarized_candidates),
        "input_group_count": payload.get("input_group_count"),
        "oversized_submission": payload.get("oversized_submission"),
        "candidate_groups": summarized_candidates,
        "abandon_reason": str(payload.get("abandon_reason", "")),
    }


def _render_summary(path: Path) -> dict[str, object]:
    if not path.exists():
        return {
            "available": False,
            "status": "unavailable",
            "groups": [],
            "summary": None,
        }
    payload = _read_object(path)
    raw_groups = payload.get("groups", [])
    groups = (
        [item for item in raw_groups if isinstance(item, dict)]
        if isinstance(raw_groups, list)
        else []
    )
    return {
        "available": True,
        "status": str(payload.get("status", "unknown")),
        "groups": groups,
        "summary": payload.get("summary"),
    }


def _load_grouping(trace_root: Path, target_date: str) -> dict[str, object]:
    merge_root = _merge_root(trace_root, target_date)
    input_payload = _read_object(merge_root / "input.json")
    resolved_payload = _read_object(merge_root / "resolved_groups.json")
    raw_candidates = input_payload.get("candidates", [])
    candidates = (
        [item for item in raw_candidates if isinstance(item, dict)]
        if isinstance(raw_candidates, list)
        else []
    )
    candidate_ids = [str(item.get("draft_id", "")) for item in candidates]
    candidate_ids = [item for item in candidate_ids if item]
    groups = _normalized_groups(resolved_payload)
    review = _review_summary(merge_root / "day_group_review.json")
    return {
        "trace_root": str(trace_root.resolve()),
        "candidate_ids": candidate_ids,
        "candidate_count": len(candidate_ids),
        "groups": groups,
        "group_count": len(groups),
        "singleton_group_count": sum(
            len(item.get("draft_ids", [])) == 1 for item in groups
        ),
        "multi_event_group_count": sum(
            len(item.get("draft_ids", [])) > 1 for item in groups
        ),
        "coverage": _coverage(candidate_ids, groups),
        "discovery": _discovery_summary(
            merge_root / "day_group_discovery.json",
            review,
        ),
        "review": review,
        "content_render": _render_summary(
            merge_root / "personal_group_render.json"
        ),
        "warnings": resolved_payload.get("warnings", []),
        "summary": resolved_payload.get("summary"),
    }


def _build_comparison(
    baseline: dict[str, object],
    current: dict[str, object],
    *,
    target_date: str,
) -> dict[str, object]:
    baseline_groups = baseline.get("groups", [])
    current_groups = current.get("groups", [])
    if not isinstance(baseline_groups, list) or not isinstance(current_groups, list):
        raise ValueError("Grouping payloads must contain group lists.")
    baseline_pairs = _group_pairs(baseline_groups)
    current_pairs = _group_pairs(current_groups)
    merged_pairs = sorted(current_pairs - baseline_pairs)
    split_pairs = sorted(baseline_pairs - current_pairs)
    baseline_membership = _membership(baseline_groups)
    current_membership = _membership(current_groups)
    candidate_ids = list(
        dict.fromkeys(
            [
                *[str(item) for item in baseline.get("candidate_ids", [])],
                *[str(item) for item in current.get("candidate_ids", [])],
            ]
        )
    )
    partition_changes = [
        {
            "draft_id": draft_id,
            "baseline_group_draft_ids": baseline_membership.get(draft_id, []),
            "current_group_draft_ids": current_membership.get(draft_id, []),
        }
        for draft_id in candidate_ids
        if baseline_membership.get(draft_id, [])
        != current_membership.get(draft_id, [])
    ]
    return {
        "target_date": target_date,
        "comparison_scope": "structure_and_relationships_only",
        "baseline": baseline,
        "current": current,
        "changes": {
            "group_count_delta": int(current.get("group_count", 0))
            - int(baseline.get("group_count", 0)),
            "singleton_group_count_delta": int(
                current.get("singleton_group_count", 0)
            )
            - int(baseline.get("singleton_group_count", 0)),
            "multi_event_group_count_delta": int(
                current.get("multi_event_group_count", 0)
            )
            - int(baseline.get("multi_event_group_count", 0)),
            "merged_candidate_pairs": [list(item) for item in merged_pairs],
            "split_candidate_pairs": [list(item) for item in split_pairs],
            "merged_candidate_ids": sorted(
                {draft_id for pair in merged_pairs for draft_id in pair}
            ),
            "split_candidate_ids": sorted(
                {draft_id for pair in split_pairs for draft_id in pair}
            ),
            "candidate_partition_changes": partition_changes,
        },
    }


def _format_ids(values: object) -> str:
    if not isinstance(values, list) or not values:
        return "-"
    return ", ".join(str(item) for item in values)


def _render_markdown(payload: dict[str, object]) -> str:
    baseline = payload["baseline"]
    current = payload["current"]
    changes = payload["changes"]
    assert isinstance(baseline, dict)
    assert isinstance(current, dict)
    assert isinstance(changes, dict)
    lines = [
        f"# {payload['target_date']} 事件分组前后对比",
        "",
        "> 本报告只比较候选覆盖和分组关系，不判断合并语义是否正确。",
        "",
        "| 指标 | 基线 | 当前 | 差值 |",
        "| --- | ---: | ---: | ---: |",
        f"| 候选数 | {baseline['candidate_count']} | {current['candidate_count']} | {int(current['candidate_count']) - int(baseline['candidate_count'])} |",
        f"| 分组数 | {baseline['group_count']} | {current['group_count']} | {changes['group_count_delta']} |",
        f"| 单例组 | {baseline['singleton_group_count']} | {current['singleton_group_count']} | {changes['singleton_group_count_delta']} |",
        f"| 多事件组 | {baseline['multi_event_group_count']} | {current['multi_event_group_count']} | {changes['multi_event_group_count_delta']} |",
        "",
        "## 覆盖检查",
        "",
        f"- 基线有效：{baseline['coverage']['valid']}；遗漏：{_format_ids(baseline['coverage']['missing_draft_ids'])}；重复：{_format_ids(baseline['coverage']['duplicate_draft_ids'])}",
        f"- 当前有效：{current['coverage']['valid']}；遗漏：{_format_ids(current['coverage']['missing_draft_ids'])}；重复：{_format_ids(current['coverage']['duplicate_draft_ids'])}",
        "",
        "## 分组变化",
        "",
        f"- 新增同组关系：{_format_ids(changes['merged_candidate_pairs'])}",
        f"- 取消同组关系：{_format_ids(changes['split_candidate_pairs'])}",
        f"- 涉及合并的候选：{_format_ids(changes['merged_candidate_ids'])}",
        f"- 涉及拆分的候选：{_format_ids(changes['split_candidate_ids'])}",
        "",
        "## 当前多事件组",
        "",
        "| 组编号 | 候选 | 主事件 | 合并理由 | 证据消息 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for group in current["groups"]:
        if len(group["draft_ids"]) <= 1:
            continue
        values = (
            group["group_id"],
            _format_ids(group["draft_ids"]),
            group["primary_draft_id"],
            group["merge_reason"] or "-",
            _format_ids(group["evidence_message_ids"]),
        )
        lines.append("| " + " | ".join(str(value).replace("|", "\\|") for value in values) + " |")
    if not any(len(group["draft_ids"]) > 1 for group in current["groups"]):
        lines.append("| - | - | - | 无多事件组 | - |")
    discovery = current.get("discovery", {})
    if isinstance(discovery, dict):
        lines.extend(
            [
                "",
                "## 标题发现",
                "",
                f"- 可用：{discovery.get('available', False)}",
                f"- 状态：{discovery.get('status', 'unavailable')}",
                f"- 协议：{discovery.get('protocol') or '旧记录不可用'}",
                f"- 逐组检查数：{discovery.get('checked_group_count') if discovery.get('checked_group_count') is not None else '不可用'}",
                f"- 候选组数：{discovery.get('candidate_group_count', '-')}",
                "",
            ]
        )
        for candidate in discovery.get("candidate_groups", []):
            if not isinstance(candidate, dict):
                continue
            reviewed_groups = candidate.get("reviewed_groups", [])
            reviewed_group_ids = [
                item.get("draft_ids", [])
                for item in reviewed_groups
                if isinstance(item, dict)
            ] if isinstance(reviewed_groups, list) else []
            lines.append(
                "- "
                f"{_format_ids(candidate.get('group_ids'))}："
                f"{candidate.get('reason') or '-'}；"
                f"复核 {candidate.get('review_final_backend')}/"
                f"{candidate.get('review_final_status')}；"
                f"结果 {_format_ids(reviewed_group_ids)}"
            )
    lines.extend(
        [
            "",
            "## 强关联复核",
            "",
            f"- 组件数：{current['review']['component_count']}",
            f"- 请求数：{current['review']['request_count']}",
            "",
        ]
    )
    for component in current["review"]["components"]:
        lines.append(
            f"- {component['component_id']}：{component['final_backend']}/{component['final_status']}；候选 {_format_ids(component['candidate_draft_ids'])}"
        )
    content_render = current.get("content_render", {})
    if isinstance(content_render, dict):
        lines.extend(
            [
                "",
                "## 锁定成员后的内容生成",
                "",
                f"- 可用：{content_render.get('available', False)}",
                f"- 状态：{content_render.get('status', 'unavailable')}",
            ]
        )
        for item in content_render.get("groups", []):
            if not isinstance(item, dict):
                continue
            result = item.get("result", {})
            result = result if isinstance(result, dict) else {}
            lines.append(
                "- "
                f"{item.get('group_id', '-')}：{item.get('status', '-')}；"
                f"成员 {_format_ids(item.get('draft_ids'))}；"
                f"标题 {result.get('topic') or '-'}；"
                f"对象 {result.get('object_hint') or '-'}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    baseline_root = Path(args.baseline_trace_root)
    current_root = Path(args.current_trace_root)
    comparison = _build_comparison(
        _load_grouping(baseline_root, args.date),
        _load_grouping(current_root, args.date),
        target_date=args.date,
    )
    output_json = (
        Path(args.output_json)
        if args.output_json
        else current_root / "event-grouping-comparison.json"
    )
    output_markdown = (
        Path(args.output_markdown)
        if args.output_markdown
        else current_root / "event-grouping-comparison.md"
    )
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_markdown.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    output_markdown.write_text(_render_markdown(comparison), encoding="utf-8")
    print(
        json.dumps(
            {
                "json": str(output_json.resolve()),
                "markdown": str(output_markdown.resolve()),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
