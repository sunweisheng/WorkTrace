from __future__ import annotations

from ..config import RuntimeConfig
from ..constants import AnchorStatus, ContextRequestType
from ..models import PersonalFactReviewBatch


_MODEL_ANCHOR_STATUSES = [
    AnchorStatus.COMPLETED.value,
    AnchorStatus.NEEDS_MORE_CONTEXT.value,
    AnchorStatus.NEEDS_ATTACHMENT_TEXT.value,
    AnchorStatus.NOT_WORK_RELATED.value,
    AnchorStatus.UNCERTAIN.value,
]
_CONTEXT_REQUEST_TYPES = [item.value for item in ContextRequestType]


def batch_output_schema(config: RuntimeConfig | None = None) -> dict[str, object]:
    runtime_config = config or RuntimeConfig()
    return {
        "type": "object",
        "properties": {
            "candidate_events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "content": {"type": "string"},
                        "action_label": {"type": "string"},
                        "object_hint": {"type": "string"},
                        "retention_reason": {
                            "type": "string",
                            "enum": [
                                "deliverable_updated",
                                "decision_made",
                                "issue_or_risk_found",
                                "follow_up_assigned",
                                "external_business_progress",
                                "substantive_approval",
                            ],
                        },
                        "retention_detail": {"type": "string"},
                        "referenced_link_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "referenced_attachment_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "self_evidence_message_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "self_relations": _self_relations_schema(runtime_config),
                        "source_message_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        **_personal_fact_properties(runtime_config),
                    },
                    "required": [
                        "topic",
                        "content",
                        "action_label",
                        "object_hint",
                        "retention_reason",
                        "retention_detail",
                        "referenced_link_ids",
                        "referenced_attachment_ids",
                        "self_evidence_message_ids",
                        "self_relations",
                        "source_message_ids",
                        "fact_items",
                        "fact_risk_flags",
                    ],
                    "additionalProperties": False,
                },
            },
            "context_requests": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "request_type": {
                            "type": "string",
                            "enum": _CONTEXT_REQUEST_TYPES,
                        },
                        "target_message_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "target_attachment_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "target_link_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": [
                        "request_type",
                        "target_message_ids",
                        "target_attachment_ids",
                        "target_link_ids",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["candidate_events", "context_requests"],
        "additionalProperties": False,
    }


def conversation_segmentation_output_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "segment_start_message_ids": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
            },
        },
        "required": ["segment_start_message_ids"],
        "additionalProperties": False,
    }


def segment_batch_output_schema(config: RuntimeConfig | None = None) -> dict[str, object]:
    runtime_config = config or RuntimeConfig()
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "segment_id": {"type": "string"},
                        "analysis": {
                            "type": "object",
                            "properties": {
                                "candidate_events": {
                                    "type": "array",
                                    "items": _segment_candidate_schema(runtime_config),
                                },
                                "context_requests": _context_request_schema(),
                            },
                            "required": ["candidate_events", "context_requests"],
                            "additionalProperties": False,
                        },
                    },
                    "required": ["segment_id", "analysis"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["results"],
        "additionalProperties": False,
    }


def retention_review_output_schema(config: RuntimeConfig) -> dict[str, object]:
    routine_types = [item.key for item in config.retention_policy.routine_signals]
    substantive_types = [
        item.key for item in config.retention_policy.substantive_signals
    ]

    def signal_schema(allowed_types: list[str]) -> dict[str, object]:
        return {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": allowed_types},
                    "evidence_message_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["type", "evidence_message_ids"],
                "additionalProperties": False,
            },
        }

    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "draft_id": {"type": "string"},
                        "routine_signals": signal_schema(routine_types),
                        "substantive_signals": signal_schema(substantive_types),
                    },
                    "required": [
                        "draft_id",
                        "routine_signals",
                        "substantive_signals",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    }


def personal_fact_review_output_schema(
    batch: PersonalFactReviewBatch,
) -> dict[str, object]:
    if len(batch.candidates) != 1:
        raise ValueError(
            "Personal fact review schema requires exactly one candidate."
        )
    candidate = batch.candidates[0]
    allowed_evidence_message_ids = list(
        dict.fromkeys(candidate.allowed_evidence_message_ids)
    )
    if not allowed_evidence_message_ids:
        raise ValueError(
            "Personal fact review schema requires candidates with allowed evidence."
        )
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "draft_id": {
                            "type": "string",
                            "enum": [candidate.candidate.draft_id],
                        },
                        "supported": {"type": "boolean"},
                        "fact_items": _personal_fact_review_items_schema(
                            allowed_evidence_message_ids
                        ),
                        "removed_claims": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": [
                        "draft_id",
                        "supported",
                        "fact_items",
                        "removed_claims",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    }


def _segment_candidate_schema(config: RuntimeConfig) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "topic": {"type": "string"},
            "content": {"type": "string"},
            "action_label": {"type": "string"},
            "object_hint": {"type": "string"},
            "retention_reason": {
                "type": "string",
                "enum": [
                    "deliverable_updated",
                    "decision_made",
                    "issue_or_risk_found",
                    "follow_up_assigned",
                    "external_business_progress",
                    "substantive_approval",
                ],
            },
            "retention_detail": {"type": "string"},
            "referenced_link_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "referenced_attachment_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "self_evidence_message_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "self_relations": _self_relations_schema(config),
            "source_message_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            **_personal_fact_properties(config),
        },
        "required": [
            "topic",
            "content",
            "action_label",
            "object_hint",
            "retention_reason",
            "retention_detail",
            "referenced_link_ids",
            "referenced_attachment_ids",
            "self_evidence_message_ids",
            "self_relations",
            "source_message_ids",
            "fact_items",
            "fact_risk_flags",
        ],
        "additionalProperties": False,
    }


def _personal_fact_review_items_schema(
    allowed_evidence_message_ids: list[str],
) -> dict[str, object]:
    single_item = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "evidence_message_ids": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": allowed_evidence_message_ids,
                },
            },
        },
        "required": ["text", "evidence_message_ids"],
        "additionalProperties": False,
    }
    field_names = (
        "topic",
        "action_label",
        "object_hint",
        "retention_detail",
    )
    return {
        "type": "object",
        "properties": {
            **{field_name: single_item for field_name in field_names},
            "content": {
                "type": "array",
                "items": single_item,
            },
        },
        "required": ["topic", "content", *field_names[1:]],
        "additionalProperties": False,
    }


def _context_request_schema() -> dict[str, object]:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "request_type": {
                    "type": "string",
                    "enum": _CONTEXT_REQUEST_TYPES,
                },
                "target_message_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "target_attachment_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "target_link_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": [
                "request_type",
                "target_message_ids",
                "target_attachment_ids",
                "target_link_ids",
            ],
            "additionalProperties": False,
        },
    }


def anchor_batch_output_schema(config: RuntimeConfig | None = None) -> dict[str, object]:
    runtime_config = config or RuntimeConfig()
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "anchor_unit_id": {"type": "string"},
                        "analysis": {
                            "type": "object",
                            "properties": {
                                "anchor_status": {
                                    "type": "string",
                                    "enum": _MODEL_ANCHOR_STATUSES,
                                },
                                "candidate_events": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "topic": {"type": "string"},
                                            "content": {"type": "string"},
                                            "action_label": {"type": "string"},
                                            "object_hint": {"type": "string"},
                                            "retention_reason": {
                                                "type": "string",
                                                "enum": [
                                                    "deliverable_updated",
                                                    "decision_made",
                                                    "issue_or_risk_found",
                                                    "follow_up_assigned",
                                                    "external_business_progress",
                                                    "substantive_approval",
                                                ],
                                            },
                                            "retention_detail": {"type": "string"},
                                            "referenced_link_ids": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                            "referenced_attachment_ids": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                            "self_evidence_message_ids": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                            "self_relations": _self_relations_schema(
                                                runtime_config
                                            ),
                                            "source_message_ids": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                            **_personal_fact_properties(runtime_config),
                                        },
                                        "required": [
                                            "topic",
                                            "content",
                                            "action_label",
                                            "object_hint",
                                            "retention_reason",
                                            "retention_detail",
                                            "referenced_link_ids",
                                            "referenced_attachment_ids",
                                            "self_evidence_message_ids",
                                            "self_relations",
                                            "source_message_ids",
                                            "fact_items",
                                            "fact_risk_flags",
                                        ],
                                        "additionalProperties": False,
                                    },
                                },
                                "context_requests": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "request_type": {
                                                "type": "string",
                                                "enum": _CONTEXT_REQUEST_TYPES,
                                            },
                                            "target_message_ids": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                            "target_attachment_ids": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                            "target_link_ids": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                        },
                                        "required": [
                                            "request_type",
                                            "target_message_ids",
                                            "target_attachment_ids",
                                            "target_link_ids",
                                        ],
                                        "additionalProperties": False,
                                    },
                                },
                                "needs_cross_anchor_merge": {"type": "boolean"},
                            },
                            "required": [
                                "anchor_status",
                                "candidate_events",
                                "context_requests",
                                "needs_cross_anchor_merge",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "required": ["anchor_unit_id", "analysis"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["results"],
        "additionalProperties": False,
    }


def anchor_output_schema(config: RuntimeConfig | None = None) -> dict[str, object]:
    batch_schema = anchor_batch_output_schema(config)
    return batch_schema["properties"]["results"]["items"]["properties"][
        "analysis"
    ]


def _self_relations_schema(config: RuntimeConfig) -> dict[str, object]:
    relation_keys = [item.key for item in config.self_relation_types]
    relation_schema: dict[str, object] = {"type": "string"}
    if relation_keys:
        relation_schema["enum"] = relation_keys
    return {
        "type": "array",
        "maxItems": len(relation_keys),
        "items": {
            "type": "object",
            "properties": {
                "relation": relation_schema,
                "evidence_message_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["relation", "evidence_message_ids"],
            "additionalProperties": False,
        },
    }


def _personal_fact_properties(config: RuntimeConfig) -> dict[str, object]:
    risk_keys = [item.key for item in config.retention_policy.fact_risk_signals]
    risk_item: dict[str, object] = {"type": "string"}
    if risk_keys:
        risk_item["enum"] = risk_keys
    return {
        "fact_items": _personal_fact_items_schema(),
        "fact_risk_flags": {
            "type": "array",
            "maxItems": len(risk_keys),
            "uniqueItems": True,
            "items": risk_item,
        },
    }


def _personal_fact_items_schema() -> dict[str, object]:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "enum": [
                        "topic",
                        "content",
                        "action_label",
                        "object_hint",
                        "retention_detail",
                    ],
                },
                "text": {"type": "string"},
                "evidence_message_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["field", "text", "evidence_message_ids"],
            "additionalProperties": False,
        },
    }


def merge_output_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "groups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "draft_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "primary_draft_id": {"type": "string"},
                        "merge_reason": {"type": "string"},
                        "evidence_message_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": [
                        "draft_ids",
                        "primary_draft_id",
                        "merge_reason",
                        "evidence_message_ids",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["groups"],
        "additionalProperties": False,
    }


def day_group_discovery_output_schema(
    group_ids: list[str],
) -> dict[str, object]:
    unique_group_ids = list(dict.fromkeys(group_ids))
    return {
        "type": "object",
        "properties": {
            "group_checks": {
                "type": "array",
                "minItems": len(unique_group_ids),
                "maxItems": len(unique_group_ids),
                "items": {
                    "type": "object",
                    "properties": {
                        "group_id": {
                            "type": "string",
                            "enum": unique_group_ids,
                        },
                        "related_group_ids": {
                            "type": "array",
                            "maxItems": max(0, len(unique_group_ids) - 1),
                            "uniqueItems": True,
                            "items": {
                                "type": "string",
                                "enum": unique_group_ids,
                            },
                        },
                        "reason": {"type": "string", "minLength": 1},
                    },
                    "required": ["group_id", "related_group_ids", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["group_checks"],
        "additionalProperties": False,
    }


def day_group_discovery_typical_arguments(
    group_ids: list[str],
) -> dict[str, object]:
    return {
        "group_checks": [
            {
                "group_id": group_id,
                "related_group_ids": [],
                "reason": "结构示例，不代表实际结论",
            }
            for group_id in dict.fromkeys(group_ids)
        ]
    }


def day_group_review_output_schema(
    config: RuntimeConfig,
    *,
    draft_ids: list[str],
    message_ids: list[str],
    relation_ids: list[str],
) -> dict[str, object]:
    schema = personal_grouping_function_schema(
        config,
        draft_ids=draft_ids,
        message_ids=message_ids,
    )
    unique_draft_ids = list(dict.fromkeys(draft_ids))
    unique_message_ids = list(dict.fromkeys(message_ids))
    unique_relation_ids = list(dict.fromkeys(relation_ids))
    properties = schema["properties"]
    assert isinstance(properties, dict)
    relation_resolution_schema = {
        "type": "array",
        "minItems": len(unique_relation_ids),
        "maxItems": len(unique_relation_ids),
        "items": {
            "type": "object",
            "properties": {
                "relation_id": {
                    "type": "string",
                    "enum": unique_relation_ids,
                },
                "decision": {
                    "type": "string",
                    "enum": ["merged", "separate"],
                    "description": (
                        "先根据关系两侧的完整内容判断：直接属于同一过程时为 merged，"
                        "具有可独立汇报的不同目标或结果时为 separate；最终分组必须与此判断一致。"
                    ),
                },
                "connected_draft_ids": {
                    "type": "array",
                    "maxItems": len(unique_draft_ids),
                    "uniqueItems": True,
                    "description": (
                        "merged 时只填写证明关系成立所需的最少成员，且所有成员必须位于"
                        "同一个最终组；直接两端关系必须包含左右两端。separate 时可以为空，"
                        "也可以填写关系两侧的代表成员；填写后代表成员必须位于不同最终组。"
                    ),
                    "items": {
                        "type": "string",
                        "enum": unique_draft_ids,
                    },
                },
                "reason": {"type": "string", "minLength": 1},
                "evidence_message_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": len(unique_message_ids),
                    "uniqueItems": True,
                    "description": (
                        "只返回直接支持当前关系判断的最少必要消息，且必须覆盖关系各侧；"
                        "不得复制检查范围的全部消息编号。"
                    ),
                    "items": {
                        "type": "string",
                        "enum": unique_message_ids,
                    },
                },
            },
            "required": [
                "relation_id",
                "decision",
                "connected_draft_ids",
                "reason",
                "evidence_message_ids",
            ],
            "additionalProperties": False,
        },
    }
    properties = {
        "relation_resolutions": relation_resolution_schema,
        **properties,
    }
    schema["properties"] = properties
    required = schema["required"]
    assert isinstance(required, list)
    required.insert(0, "relation_resolutions")
    return schema


def personal_group_render_output_schema(
    *,
    group_id: str,
    draft_ids: list[str],
    message_ids: list[str],
) -> dict[str, object]:
    unique_draft_ids = list(dict.fromkeys(draft_ids))
    unique_message_ids = list(dict.fromkeys(message_ids))
    return {
        "type": "object",
        "properties": {
            "groups": {
                "type": "array",
                "minItems": 1,
                "maxItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "group_id": {"type": "string", "enum": [group_id]},
                        "covered_draft_ids": {
                            "type": "array",
                            "minItems": len(unique_draft_ids),
                            "maxItems": len(unique_draft_ids),
                            "uniqueItems": True,
                            "items": {
                                "type": "string",
                                "enum": unique_draft_ids,
                            },
                        },
                        "fact_items": {
                            "type": "array",
                            "minItems": 3,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "field": {
                                        "type": "string",
                                        "enum": ["topic", "content", "object_hint"],
                                    },
                                    "text": {"type": "string", "minLength": 1},
                                    "evidence_message_ids": {
                                        "type": "array",
                                        "minItems": 1,
                                        "uniqueItems": True,
                                        "items": {
                                            "type": "string",
                                            "enum": unique_message_ids,
                                        },
                                    },
                                },
                                "required": [
                                    "field",
                                    "text",
                                    "evidence_message_ids",
                                ],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": [
                        "group_id",
                        "covered_draft_ids",
                        "fact_items",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["groups"],
        "additionalProperties": False,
    }


def personal_grouping_function_schema(
    config: RuntimeConfig,
    *,
    draft_ids: list[str],
    message_ids: list[str],
) -> dict[str, object]:
    unique_draft_ids = list(dict.fromkeys(draft_ids))
    unique_message_ids = list(dict.fromkeys(message_ids))
    semantic_reasons = [
        item.key
        for item in config.collected_group_reason_definitions
        if item.supports_semantic_merge and not item.evidence_relation
    ]
    group_schema = {
        "type": "object",
        "properties": {
            "draft_ids": {
                "type": "array",
                "minItems": 2,
                "maxItems": len(unique_draft_ids),
                "uniqueItems": True,
                "items": {"type": "string", "enum": unique_draft_ids},
            },
            "primary_draft_id": {
                "type": "string",
                "enum": unique_draft_ids,
            },
            "common_object": {"type": "string", "minLength": 1},
            "semantic_reasons": {
                "type": "array",
                "minItems": 1,
                "maxItems": len(semantic_reasons),
                "uniqueItems": True,
                "items": {"type": "string", "enum": semantic_reasons},
            },
            "reason_detail": {"type": "string", "minLength": 1},
            "member_connections": {
                "type": "array",
                "minItems": 2,
                "maxItems": len(unique_draft_ids),
                "items": {
                    "type": "object",
                    "properties": {
                        "draft_id": {
                            "type": "string",
                            "enum": unique_draft_ids,
                        },
                        "connection_detail": {
                            "type": "string",
                            "minLength": 1,
                        },
                        "evidence_message_ids": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {
                                "type": "string",
                                "enum": unique_message_ids,
                            },
                        },
                    },
                    "required": [
                        "draft_id",
                        "connection_detail",
                        "evidence_message_ids",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": [
            "draft_ids",
            "primary_draft_id",
            "common_object",
            "semantic_reasons",
            "reason_detail",
            "member_connections",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "merged_groups": {
                "type": "array",
                "minItems": 0,
                "maxItems": len(unique_draft_ids) // 2,
                "description": (
                    "只填写包含至少两个成员的最终组；与 singleton_draft_ids 合并后"
                    "必须完整且不重复地覆盖全部合法 draft_id。"
                ),
                "items": group_schema,
            },
            "singleton_draft_ids": {
                "type": "array",
                "minItems": 0,
                "maxItems": len(unique_draft_ids),
                "uniqueItems": True,
                "description": (
                    "填写所有未进入 merged_groups 的合法 draft_id；两部分合并后"
                    "必须完整且不重复地覆盖全部合法 draft_id。"
                ),
                "items": {"type": "string", "enum": unique_draft_ids},
            },
        },
        "required": ["merged_groups", "singleton_draft_ids"],
        "additionalProperties": False,
    }


def collected_grouping_output_schema(
    config: RuntimeConfig | None = None,
) -> dict[str, object]:
    runtime_config = config or RuntimeConfig()
    reason_definitions = runtime_config.collected_group_reason_definitions
    return {
        "type": "object",
        "properties": {
            "split_reason": {
                "type": "string",
                "description": (
                    "高风险复核拆成多个组时，填写一条能够说明整体分组差异的原因；"
                    "未拆分或普通候选分组时返回空字符串。"
                ),
            },
            "groups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "group_id": {"type": "string"},
                        "draft_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "summary_title": {"type": "string"},
                        "summary_content": {"type": "string"},
                        "summary_object_hint": {"type": "string"},
                        "group_reason": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": [item.key for item in reason_definitions],
                            },
                        },
                        "risk_flags": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": [
                                    "cross_batch",
                                    "broad_object",
                                    "large_group",
                                ],
                            },
                        },
                    },
                    "required": [
                        "group_id",
                        "draft_ids",
                        "summary_title",
                        "summary_content",
                        "summary_object_hint",
                        "group_reason",
                        "risk_flags",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["split_reason", "groups"],
        "additionalProperties": False,
    }


def collected_grouping_function_schema(
    config: RuntimeConfig,
    *,
    draft_ids: list[str],
    include_split_reason: bool,
    relation_ids: list[str] | None = None,
) -> dict[str, object]:
    unique_draft_ids = list(dict.fromkeys(draft_ids))
    semantic_reasons = [
        item.key
        for item in config.collected_group_reason_definitions
        if item.supports_semantic_merge and not item.evidence_relation
    ]
    risk_flags = [
        "cross_batch",
        "broad_object",
        "large_group",
    ]
    semantic_item_schema: dict[str, object] = {"type": "string"}
    if semantic_reasons:
        semantic_item_schema["enum"] = semantic_reasons

    group_schema = {
        "type": "object",
        "properties": {
            "group_id": {
                "type": "string",
                "minLength": 1,
                "description": "只命名最终有效组，不能用于传递错误、诊断或已取消方案。",
            },
            "draft_ids": {
                "type": "array",
                "minItems": 2,
                "maxItems": len(unique_draft_ids),
                "uniqueItems": True,
                "description": (
                    "当前最终组的全部成员，至少两条；不得再出现在其他组或 singleton_draft_ids。"
                ),
                "items": {"type": "string", "enum": unique_draft_ids},
            },
            "summary_title": {"type": "string", "minLength": 1},
            "summary_content": {"type": "string", "minLength": 1},
            "summary_object_hint": {"type": "string", "minLength": 1},
            "semantic_reasons": {
                "type": "array",
                "maxItems": len(semantic_reasons),
                "uniqueItems": True,
                "items": semantic_item_schema,
            },
            "member_connections": {
                "type": "array",
                "minItems": 2,
                "maxItems": len(unique_draft_ids),
                "items": {
                    "type": "object",
                    "properties": {
                        "draft_id": {
                            "type": "string",
                            "enum": unique_draft_ids,
                        },
                        "connection_detail": {"type": "string", "minLength": 1},
                    },
                    "required": ["draft_id", "connection_detail"],
                    "additionalProperties": False,
                },
            },
            "reason_detail": {"type": "string", "minLength": 1},
            "risk_flags": {
                "type": "array",
                "maxItems": len(risk_flags),
                "uniqueItems": True,
                "items": {"type": "string", "enum": risk_flags},
            },
        },
        "required": [
            "group_id",
            "draft_ids",
            "summary_title",
            "summary_content",
            "summary_object_hint",
            "semantic_reasons",
            "member_connections",
            "reason_detail",
            "risk_flags",
        ],
        "additionalProperties": False,
    }
    properties: dict[str, object] = {
        "merged_groups": {
            "type": "array",
            "minItems": 0,
            "maxItems": len(unique_draft_ids) // 2,
            "description": (
                "只返回最终确认成立的多事件组；已取消、拟拆分、单成员、诊断占位组，"
                "以及仅用于表示候选关系应分开的组一律不返回。"
            ),
            "items": group_schema,
        },
        "singleton_draft_ids": {
            "type": "array",
            "minItems": 0,
            "maxItems": len(unique_draft_ids),
            "uniqueItems": True,
            "description": (
                "未进入最终多事件组的输入编号，每个恰好一次；不得包含已在 merged_groups 中的编号。"
            ),
            "items": {"type": "string", "enum": unique_draft_ids},
        },
    }
    required = ["merged_groups", "singleton_draft_ids"]
    if include_split_reason:
        properties = {"split_reason": {"type": "string"}, **properties}
        required = ["split_reason", *required]
    unique_relation_ids = list(dict.fromkeys(relation_ids or []))
    if unique_relation_ids:
        relation_resolution_schema = {
            "type": "array",
            "minItems": len(unique_relation_ids),
            "maxItems": len(unique_relation_ids),
            "items": {
                "type": "object",
                "properties": {
                    "relation_id": {
                        "type": "string",
                        "enum": unique_relation_ids,
                    },
                    "decision": {
                        "type": "string",
                        "enum": ["merged", "separate"],
                        "description": (
                            "先根据关系两侧的完整内容判断：直接属于同一过程时为 merged，"
                            "具有可独立汇报的不同目标或结果时为 separate；最终分组必须与此判断一致。"
                        ),
                    },
                    "connected_draft_ids": {
                        "type": "array",
                        "maxItems": len(unique_draft_ids),
                        "uniqueItems": True,
                        "description": (
                            "merged 时只填写证明关系成立所需的最少成员，且所有成员必须位于"
                            "同一个最终组；直接两端关系必须包含左右两端。separate 时可以为空，"
                            "也可以填写关系两侧的代表成员；填写后代表成员必须位于不同最终组。"
                        ),
                        "items": {
                            "type": "string",
                            "enum": unique_draft_ids,
                        },
                    },
                    "reason": {"type": "string", "minLength": 1},
                    "evidence_draft_ids": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": len(unique_draft_ids),
                        "uniqueItems": True,
                        "description": "必须覆盖当前关系各侧的候选事件证据。",
                        "items": {
                            "type": "string",
                            "enum": unique_draft_ids,
                        },
                    },
                },
                "required": [
                    "relation_id",
                    "decision",
                    "connected_draft_ids",
                    "reason",
                    "evidence_draft_ids",
                ],
                "additionalProperties": False,
            },
        }
        properties = {
            "relation_resolutions": relation_resolution_schema,
            **properties,
        }
        required = ["relation_resolutions", *required]
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def collected_merge_output_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "groups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "group_id": {"type": "string"},
                        "draft_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                        "object_hint": {"type": "string"},
                        "retention_reason": {
                            "type": "string",
                            "enum": [
                                "deliverable_updated",
                                "decision_made",
                                "issue_or_risk_found",
                                "follow_up_assigned",
                                "external_business_progress",
                                "substantive_approval",
                            ],
                        },
                        "retention_detail": {"type": "string"},
                        "merge_owner_conflict": {"type": "boolean"},
                        "conflict_detail": {"type": "string"},
                        "covered_draft_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "fact_items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "text": {"type": "string"},
                                    "source_draft_ids": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                                "required": ["text", "source_draft_ids"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": [
                        "group_id",
                        "draft_ids",
                        "title",
                        "content",
                        "object_hint",
                        "retention_reason",
                        "retention_detail",
                        "merge_owner_conflict",
                        "conflict_detail",
                        "covered_draft_ids",
                        "fact_items",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["groups"],
        "additionalProperties": False,
    }
