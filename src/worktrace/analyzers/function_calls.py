from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

from .collected_evidence import EvidenceRelation


COLLECTED_GROUPING_FINAL_PARAMETER_CHECKS = (
    "只提交最终决定，不保留分析过程、备选方案或已否定的分组方案。",
    "先确定所有依据成立的多事件组；每组至少两条且字段完整，不得用 group_id 或空字段返回错误说明、诊断项或占位组。",
    "如果说明已经否定某个合并组，必须从 merged_groups 中实际删除该组。",
    "candidate_discovery_context 中对比后判定分开的组合不产生组对象；只通过各事件的最终归属表达结果。",
    "再把未进入任何合法多事件组的事件放入 singleton_draft_ids，不要为单条事件保留 merged_groups 对象。",
    "不得为了消除重复而扩大其他 merged_groups 的成员范围，也不得让同一事件同时出现在两部分。",
    "提交前核对全部输入 draft_id，合计必须恰好出现一次。",
)


DEFAULT_FUNCTION_CONTRACTS_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "llm_function_contracts.json"
)


@lru_cache(maxsize=1)
def _load_function_contracts() -> tuple[dict[str, dict[str, object]], tuple[str, ...]]:
    try:
        payload = json.loads(DEFAULT_FUNCTION_CONTRACTS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("LLM Function contract configuration is missing.") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("LLM Function contract configuration is not valid JSON.") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("LLM Function contract configuration has an unsupported version.")
    raw_functions = payload.get("functions")
    raw_instructions = payload.get("codex_submission_instructions")
    if not isinstance(raw_functions, dict) or not isinstance(raw_instructions, list):
        raise ValueError("LLM Function contract configuration has invalid fields.")
    functions: dict[str, dict[str, object]] = {}
    for request_kind, raw_contract in raw_functions.items():
        if not isinstance(request_kind, str) or not isinstance(raw_contract, dict):
            raise ValueError("LLM Function contract entries must be objects.")
        if set(raw_contract) != {"name", "description", "strict"}:
            raise ValueError(f"Invalid Function contract fields for {request_kind}.")
        name = raw_contract.get("name")
        description = raw_contract.get("description")
        strict = raw_contract.get("strict")
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(description, str)
            or not description.strip()
            or not isinstance(strict, bool)
        ):
            raise ValueError(f"Invalid Function contract values for {request_kind}.")
        functions[request_kind] = {
            "name": name.strip(),
            "description": description.strip(),
            "strict": strict,
        }
    if not all(isinstance(item, str) and item.strip() for item in raw_instructions):
        raise ValueError("Codex submission instructions must be non-empty strings.")
    return functions, tuple(item.strip() for item in raw_instructions)


@dataclass(frozen=True)
class FunctionCallSpec:
    request_kind: str
    name: str
    description: str
    parameters: dict[str, object]
    typical_arguments: dict[str, object]
    strict: bool = True
    argument_structure_example: dict[str, object] | None = None
    final_parameter_checks: tuple[str, ...] = ()
    codex_submission_instructions: tuple[str, ...] = ()

    def tool(self) -> dict[str, object]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "strict": self.strict,
        }

    def online_tool(self) -> dict[str, object]:
        tool = copy.deepcopy(self.tool())
        _remove_schema_keyword(tool["parameters"], "uniqueItems")
        _remove_impossible_array_min_items(tool["parameters"])
        return tool

    def tool_choice(self) -> dict[str, str]:
        return {"type": "function", "name": self.name}

    def prompt_with_example(self, prompt: str) -> str:
        return self._prepare_prompt(prompt, include_codex_contract=False)

    def codex_prompt(self, prompt: str) -> str:
        return self._prepare_prompt(prompt, include_codex_contract=True)

    def contract_payload(self) -> dict[str, object]:
        return {
            "request_kind": self.request_kind,
            "name": self.name,
            "description": self.description,
            "strict": self.strict,
            "parameters": copy.deepcopy(self.parameters),
            "typical_arguments": copy.deepcopy(self.typical_arguments),
            "argument_structure_example": copy.deepcopy(
                self.argument_structure_example
            ),
            "final_parameter_checks": list(self.final_parameter_checks),
            "codex_submission_instructions": list(
                self.codex_submission_instructions
            ),
        }

    def _prepare_prompt(self, prompt: str, *, include_codex_contract: bool) -> str:
        try:
            payload = json.loads(prompt)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            payload.pop("required_output_schema", None)
            payload["typical_function_arguments_note"] = (
                "仅用于展示字段结构；其中的分组、decision 和理由都是占位值，"
                "不代表当前输入的结论，不得复制。"
            )
            payload["typical_function_arguments"] = self.typical_arguments
            if self.argument_structure_example is not None:
                payload["function_argument_structure_example"] = (
                    self.argument_structure_example
                )
            if include_codex_contract:
                payload["function_contract"] = {
                    "name": self.name,
                    "description": self.description,
                    "strict": self.strict,
                }
                payload["codex_submission_instructions"] = list(
                    self.codex_submission_instructions
                )
            prepared = json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        else:
            prepared = (
                prompt.rstrip()
                + "\n\n典型 Function 参数示例：\n"
                + json.dumps(self.typical_arguments, ensure_ascii=False, indent=2)
            )
            if self.argument_structure_example is not None:
                prepared += (
                    "\n\nFunction 参数结构示例：\n"
                    + json.dumps(
                        self.argument_structure_example,
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            if include_codex_contract:
                prepared += (
                    "\n\nFunction contract:\n"
                    + json.dumps(
                        {
                            "name": self.name,
                            "description": self.description,
                            "strict": self.strict,
                            "codex_submission_instructions": list(
                                self.codex_submission_instructions
                            ),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
        if self.final_parameter_checks:
            prepared += (
                "\n\n最终 Function 参数自检：\n"
                + "\n".join(
                    f"{index}. {instruction}"
                    for index, instruction in enumerate(
                        self.final_parameter_checks,
                        start=1,
                    )
                )
            )
        return prepared


def _remove_schema_keyword(value: object, keyword: str) -> None:
    if isinstance(value, dict):
        value.pop(keyword, None)
        for child in value.values():
            _remove_schema_keyword(child, keyword)
    elif isinstance(value, list):
        for child in value:
            _remove_schema_keyword(child, keyword)


def _remove_impossible_array_min_items(value: object) -> None:
    if isinstance(value, dict):
        item_schema = value.get("items")
        enum_values = item_schema.get("enum") if isinstance(item_schema, dict) else None
        min_items = value.get("minItems")
        max_items = value.get("maxItems")
        if (
            value.get("type") == "array"
            and isinstance(min_items, int)
            and not isinstance(min_items, bool)
            and (
                (isinstance(enum_values, list) and min_items > len(enum_values))
                or (
                    isinstance(max_items, int)
                    and not isinstance(max_items, bool)
                    and min_items > max_items
                )
            )
        ):
            value.pop("minItems", None)
        for child in value.values():
            _remove_impossible_array_min_items(child)
    elif isinstance(value, list):
        for child in value:
            _remove_impossible_array_min_items(child)


@dataclass(frozen=True)
class CollectedGroupingCallContract:
    function_spec: FunctionCallSpec
    evidence_catalog: tuple[EvidenceRelation, ...]
    semantic_reasons: tuple[str, ...]


@dataclass(frozen=True)
class PersonalGroupingCallContract:
    function_spec: FunctionCallSpec
    semantic_reasons: tuple[str, ...]


def personal_grouping_call_contract(
    *,
    config: object,
    candidates: Sequence[object],
) -> PersonalGroupingCallContract:
    from .output_schemas import personal_grouping_function_schema

    draft_ids = [str(getattr(item, "draft_id")) for item in candidates]
    message_ids = list(
        dict.fromkeys(
            str(message_id)
            for item in candidates
            for message_id in getattr(item, "source_message_ids", [])
            if str(message_id).strip()
        )
    )
    semantic_reasons = tuple(
        str(getattr(item, "key"))
        for item in getattr(config, "collected_group_reason_definitions", ())
        if getattr(item, "supports_semantic_merge", False)
        and not getattr(item, "evidence_relation", "")
    )
    function_spec = function_call_spec(
        "day_candidate_merge",
        personal_grouping_function_schema(
            config,
            draft_ids=draft_ids,
            message_ids=message_ids,
        ),
        typical_arguments={
            "merged_groups": [],
            "singleton_draft_ids": draft_ids,
        },
    )
    return PersonalGroupingCallContract(
        function_spec=function_spec,
        semantic_reasons=semantic_reasons,
    )


def collected_grouping_call_contract(
    request_kind: str,
    *,
    config: object,
    events: list[object],
    deterministic_groups: list[list[str]],
    include_split_reason: bool,
    relation_ids: list[str] | None = None,
    final_parameter_checks: Sequence[str] = (),
) -> CollectedGroupingCallContract:
    from .output_schemas import collected_grouping_function_schema
    from .prompts import build_collected_evidence_relation_catalog

    catalog = build_collected_evidence_relation_catalog(
        events,
        excluded_draft_ids=set(),
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
    example_groups = (
        [] if request_kind == "collected_group_review" else deterministic_groups
    )
    for index, group in enumerate(example_groups, start=1):
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
                "reason_detail": "这些记录描述同一具体事项的连续动作。",
                "member_connections": [
                    {
                        "draft_id": draft_id,
                        "connection_detail": "该记录属于同一确定性事件组。",
                    }
                    for draft_id in members
                ],
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
            "split_reason": (
                "示例中的记录缺少足够的同一事项依据，因此分别保留。"
                if request_kind == "collected_group_review"
                else ""
            ),
            **typical_arguments,
        }
    unique_relation_ids = list(dict.fromkeys(relation_ids or []))
    relation_final_checks = (
        (
            "strong_relations 非空时，relation_resolutions 是必填列表；"
            "即使最终分组已经表达合并或拆分结论，也不得省略。"
        ),
        (
            "relation_resolutions 中的 relation_id 必须与本次要求完全一致，"
            "每个编号恰好一次："
            + json.dumps(unique_relation_ids, ensure_ascii=False)
            + "。"
        ),
        (
            "merged_groups、singleton_draft_ids 和 split_reason 不能代替逐条关系判断；"
            "提交前单独核对 relation_resolutions 的数量和编号。"
        ),
    ) if unique_relation_ids else ()
    if unique_relation_ids:
        typical_arguments = {
            "relation_resolutions": [
                {
                    "relation_id": relation_id,
                    "decision": "separate",
                    "connected_draft_ids": [],
                    "reason": "结构占位理由，不代表当前关系应当分开。",
                    "evidence_draft_ids": draft_ids[:2] or draft_ids[:1],
                }
                for relation_id in unique_relation_ids
            ],
            **typical_arguments,
        }
    argument_structure_example = None
    if not typical_groups:
        argument_structure_example = {
            "note": (
                "仅展示一致的最终参数结构；占位值不属于输入，也不表示任何实际事件应合并。"
                "若另一个候选组合经对比后应分开，不要增加第二个 merged_groups 项。"
            ),
            "valid_final_arguments": {
                "merged_groups": [
                    {
                        "group_id": "<group_id>",
                        "draft_ids": ["<input_draft_id_1>", "<input_draft_id_2>"],
                        "summary_title": "<summary_title>",
                        "summary_content": "<summary_content>",
                        "summary_object_hint": "<summary_object_hint>",
                        "semantic_reasons": ["<allowed_semantic_reason>"],
                        "reason_detail": "<reason_detail>",
                        "member_connections": [
                            {
                                "draft_id": "<input_draft_id_1>",
                                "connection_detail": "<direct_connection_detail>",
                            },
                            {
                                "draft_id": "<input_draft_id_2>",
                                "connection_detail": "<direct_connection_detail>",
                            },
                        ],
                        "risk_flags": [],
                    }
                ],
                "singleton_draft_ids": ["<input_draft_id_3>"],
            },
        }
    spec = function_call_spec(
        request_kind,
        collected_grouping_function_schema(
            config,
            draft_ids=draft_ids,
            include_split_reason=include_split_reason,
            relation_ids=unique_relation_ids,
        ),
        typical_arguments=typical_arguments,
        argument_structure_example=argument_structure_example,
        final_parameter_checks=(
            *relation_final_checks,
            *final_parameter_checks,
        ),
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
    argument_structure_example: dict[str, object] | None = None,
    final_parameter_checks: Sequence[str] = (),
    enum_values: Mapping[str, Sequence[str]] | None = None,
    exact_array_lengths: Mapping[str, int] | None = None,
) -> FunctionCallSpec:
    contracts, codex_submission_instructions = _load_function_contracts()
    try:
        metadata = contracts[request_kind]
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
        name=str(metadata["name"]),
        description=str(metadata["description"]),
        strict=bool(metadata["strict"]),
        parameters=normalized_parameters,
        typical_arguments=(
            copy.deepcopy(typical_arguments)
            if typical_arguments is not None
            else _example_from_schema(normalized_parameters)
        ),
        argument_structure_example=(
            copy.deepcopy(argument_structure_example)
            if argument_structure_example is not None
            else None
        ),
        final_parameter_checks=tuple(final_parameter_checks),
        codex_submission_instructions=codex_submission_instructions,
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
