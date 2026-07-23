from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .collected_evidence import EvidenceRelation


_FUNCTION_METADATA = {
    "batch_analysis": (
        "submit_batch_analysis",
        "提交当前聊天批次提炼出的候选工作事件和补充上下文请求。",
    ),
    "conversation_segmentation": (
        "submit_conversation_segmentation",
        "提交当前会话中各独立会话轮次的起点。",
    ),
    "segment_batch_analysis": (
        "submit_segment_batch_analysis",
        "提交当前多个独立会话轮次的事件提炼结果。",
    ),
    "retention_review": (
        "submit_retention_review",
        "提交临时协作候选的信号复核结果。",
    ),
    "personal_fact_review": (
        "submit_personal_fact_review",
        "提交单个个人事件候选的事实证据复核结果。",
    ),
    "anchor_batch_analysis": (
        "submit_anchor_batch_analysis",
        "提交分段失败后的锚点批量事件提炼结果。",
    ),
    "anchor_analysis": (
        "submit_anchor_analysis",
        "提交一个锚点聊天窗口的事件提炼或上下文补读结果。",
    ),
    "day_candidate_merge": (
        "submit_day_candidate_groups",
        "提交同一天候选事件的跨会话分组结果。",
    ),
    "workstream_assignment": (
        "submit_workstream_assignments",
        "提交候选事件的工作流归属结果。",
    ),
    "unassigned_workstream_assignment": (
        "submit_unassigned_workstream_assignments",
        "提交未归属候选的工作流复核结果。",
    ),
    "collected_candidate_grouping": (
        "submit_collected_grouping_result",
        "提交多人事件候选分组，完整覆盖每个来源事件且不得重复。",
    ),
    "collected_group_review": (
        "submit_collected_group_review_result",
        "提交一个高风险多人候选组的复核或拆分结果。",
    ),
    "collected_event_merge": (
        "submit_collected_render_result",
        "提交多人分组的正式汇总内容和事实来源。",
    ),
    "reaction_metadata": (
        "submit_reaction_metadata",
        "提交飞书表情类型的结构化元数据。",
    ),
    "preflight": (
        "submit_worktrace_probe",
        "提交 WorkTrace Function Calling 能力探测结果。",
    ),
}


@dataclass(frozen=True)
class FunctionCallSpec:
    request_kind: str
    name: str
    description: str
    parameters: dict[str, object]
    typical_arguments: dict[str, object]

    def tool(self) -> dict[str, object]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "strict": True,
        }

    def tool_choice(self) -> dict[str, str]:
        return {"type": "function", "name": self.name}

    def prompt_with_example(self, prompt: str) -> str:
        try:
            payload = json.loads(prompt)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            payload.pop("required_output_schema", None)
            payload["typical_function_arguments"] = self.typical_arguments
            return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        return (
            prompt.rstrip()
            + "\n\n典型 Function 参数示例：\n"
            + json.dumps(self.typical_arguments, ensure_ascii=False, indent=2)
        )


@dataclass(frozen=True)
class CollectedGroupingCallContract:
    function_spec: FunctionCallSpec
    evidence_catalog: tuple[EvidenceRelation, ...]
    semantic_reasons: tuple[str, ...]


def collected_grouping_call_contract(
    request_kind: str,
    *,
    config: object,
    events: list[object],
    deterministic_groups: list[list[str]],
    include_split_reason: bool,
) -> CollectedGroupingCallContract:
    from .output_schemas import collected_grouping_function_schema
    from .prompts import build_collected_evidence_relation_catalog

    excluded_draft_ids = (
        set()
        if include_split_reason
        else {draft_id for group in deterministic_groups for draft_id in group}
    )
    catalog = build_collected_evidence_relation_catalog(
        events,
        excluded_draft_ids=excluded_draft_ids,
    )
    draft_ids = [str(getattr(item, "draft_id")) for item in events]
    event_by_id = {str(getattr(item, "draft_id")): item for item in events}
    semantic_reasons = [
        str(getattr(item, "key"))
        for item in getattr(config, "collected_group_reason_definitions", ())
        if getattr(item, "supports_semantic_merge", False)
        and not getattr(item, "evidence_relation", "")
    ]
    typical_groups: list[dict[str, object]] = []
    grouped_ids: set[str] = set()
    for index, group in enumerate(deterministic_groups, start=1):
        members = [draft_id for draft_id in group if draft_id in event_by_id]
        if len(members) < 2:
            continue
        grouped_ids.update(members)
        representative = event_by_id[members[0]]
        event = getattr(representative, "event", representative)
        typical_groups.append(
            {
                "group_id": f"group-{index:03d}",
                "draft_ids": members,
                "summary_title": str(getattr(event, "title", "同一事项")) or "同一事项",
                "summary_content": str(getattr(event, "content", "同一事项的连续记录。"))
                or "同一事项的连续记录。",
                "summary_object_hint": str(getattr(event, "object_hint", "具体事项"))
                or "具体事项",
                "semantic_reasons": semantic_reasons[:1],
                "evidence_relation_ids": [],
                "reason_detail": "这些记录描述同一具体事项的连续动作。",
                "risk_flags": [],
            }
        )
    typical_arguments: dict[str, object] = {
        "merged_groups": typical_groups,
        "singleton_draft_ids": [
            draft_id for draft_id in draft_ids if draft_id not in grouped_ids
        ],
    }
    if include_split_reason:
        typical_arguments = {
            "split_reason": "",
            **typical_arguments,
        }
    spec = function_call_spec(
        request_kind,
        collected_grouping_function_schema(
            config,
            draft_ids=draft_ids,
            evidence_relation_ids=[item.relation_id for item in catalog],
            include_split_reason=include_split_reason,
        ),
        typical_arguments=typical_arguments,
    )
    return CollectedGroupingCallContract(
        function_spec=spec,
        evidence_catalog=tuple(catalog),
        semantic_reasons=tuple(semantic_reasons),
    )


def function_call_spec(
    request_kind: str,
    parameters: dict[str, object],
    *,
    typical_arguments: dict[str, object] | None = None,
    enum_values: Mapping[str, Sequence[str]] | None = None,
    exact_array_lengths: Mapping[str, int] | None = None,
) -> FunctionCallSpec:
    try:
        name, description = _FUNCTION_METADATA[request_kind]
    except KeyError as exc:
        raise ValueError(f"No Function Calling definition for request kind: {request_kind}") from exc
    normalized_parameters = copy.deepcopy(parameters)
    _specialize_schema(
        normalized_parameters,
        enum_values=enum_values or {},
        exact_array_lengths=exact_array_lengths or {},
    )
    return FunctionCallSpec(
        request_kind=request_kind,
        name=name,
        description=description,
        parameters=normalized_parameters,
        typical_arguments=(
            copy.deepcopy(typical_arguments)
            if typical_arguments is not None
            else _example_from_schema(normalized_parameters)
        ),
    )


def task_function_call_spec(
    request_kind: str,
    parameters: dict[str, object],
    *,
    draft_ids: Sequence[str] = (),
    segment_ids: Sequence[str] = (),
    anchor_unit_ids: Sequence[str] = (),
    message_ids: Sequence[str] = (),
    attachment_ids: Sequence[str] = (),
    link_ids: Sequence[str] = (),
    workflow_ids: Sequence[str] = (),
    result_count: int | None = None,
    exact_array_lengths: Mapping[str, int] | None = None,
    enum_values: Mapping[str, Sequence[str]] | None = None,
    typical_arguments: dict[str, object] | None = None,
) -> FunctionCallSpec:
    enum_values = {
        "draft_id": draft_ids,
        "draft_ids": draft_ids,
        "covered_draft_ids": draft_ids,
        "source_draft_ids": draft_ids,
        "primary_draft_id": draft_ids,
        "parent_draft_id": draft_ids,
        "segment_id": segment_ids,
        "anchor_unit_id": anchor_unit_ids,
        "segment_start_message_ids": message_ids,
        "source_message_ids": message_ids,
        "self_evidence_message_ids": message_ids,
        "evidence_message_ids": message_ids,
        "target_message_ids": message_ids,
        "referenced_attachment_ids": attachment_ids,
        "target_attachment_ids": attachment_ids,
        "referenced_link_ids": link_ids,
        "target_link_ids": link_ids,
        "workflow_id": workflow_ids,
        **dict(enum_values or {}),
    }
    lengths = dict(exact_array_lengths or {})
    if result_count is not None:
        lengths["results"] = result_count
    return function_call_spec(
        request_kind,
        parameters,
        typical_arguments=typical_arguments,
        enum_values=enum_values,
        exact_array_lengths=lengths,
    )


def message_reference_ids(messages: Sequence[object]) -> dict[str, list[str]]:
    message_ids = list(
        dict.fromkeys(
            str(getattr(message, "message_id"))
            for message in messages
            if str(getattr(message, "message_id", "")).strip()
        )
    )
    attachment_ids = list(
        dict.fromkeys(
            str(getattr(attachment, "attachment_id"))
            for message in messages
            for attachment in getattr(message, "attachments", [])
            if str(getattr(attachment, "attachment_id", "")).strip()
        )
    )
    link_ids = list(
        dict.fromkeys(
            str(getattr(link, "link_id"))
            for message in messages
            for link in getattr(message, "links", [])
            if str(getattr(link, "link_id", "")).strip()
        )
    )
    return {
        "message_ids": message_ids,
        "attachment_ids": attachment_ids,
        "link_ids": link_ids,
    }


def _specialize_schema(
    schema: dict[str, object],
    *,
    enum_values: Mapping[str, Sequence[str]],
    exact_array_lengths: Mapping[str, int],
) -> None:
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for property_name, property_schema in properties.items():
            if not isinstance(property_name, str) or not isinstance(property_schema, dict):
                continue
            has_enum_constraint = property_name in enum_values
            values = list(dict.fromkeys(enum_values.get(property_name, ())))
            if has_enum_constraint:
                if property_schema.get("type") == "string":
                    if values:
                        property_schema["enum"] = values
                elif property_schema.get("type") == "array":
                    items = property_schema.get("items")
                    if isinstance(items, dict) and items.get("type") == "string":
                        if values:
                            items["enum"] = values
                        else:
                            items.pop("enum", None)
                        property_schema["uniqueItems"] = True
                        property_schema["maxItems"] = len(values)
            if property_name in exact_array_lengths:
                length = exact_array_lengths[property_name]
                if property_schema.get("type") == "array":
                    property_schema["minItems"] = length
                    property_schema["maxItems"] = length
            _specialize_schema(
                property_schema,
                enum_values=enum_values,
                exact_array_lengths=exact_array_lengths,
            )
    items = schema.get("items")
    if isinstance(items, dict):
        _specialize_schema(
            items,
            enum_values=enum_values,
            exact_array_lengths=exact_array_lengths,
        )


def _example_from_schema(schema: dict[str, object]) -> Any:
    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and enum_values:
        return enum_values[0]
    schema_type = schema.get("type")
    if schema_type == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            return {}
        return {
            key: _example_from_schema(value)
            for key in required
            if isinstance(key, str)
            and isinstance((value := properties.get(key)), dict)
        }
    if schema_type == "array":
        minimum = schema.get("minItems", 0)
        if not isinstance(minimum, int) or minimum <= 0:
            return []
        items = schema.get("items")
        item_schema = items if isinstance(items, dict) else {}
        return [_example_from_schema(item_schema) for _ in range(minimum)]
    if schema_type == "boolean":
        return False
    if schema_type in {"number", "integer"}:
        return 0
    return ""
