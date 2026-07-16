from __future__ import annotations

from ..config import RuntimeConfig


def batch_output_schema() -> dict[str, object]:
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
                        "self_relations": _self_relations_schema(),
                        "workstream_key": {"type": "string"},
                        "source_message_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
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
                        "workstream_key",
                        "source_message_ids",
                    ],
                    "additionalProperties": False,
                },
            },
            "context_requests": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "request_type": {"type": "string"},
                        "target_message_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "target_attachment_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": [
                        "request_type",
                        "target_message_ids",
                        "target_attachment_ids",
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


def segment_batch_output_schema() -> dict[str, object]:
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
                                    "items": _segment_candidate_schema(),
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


def _segment_candidate_schema() -> dict[str, object]:
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
            "self_relations": _self_relations_schema(),
            "workstream_key": {"type": "string"},
            "source_message_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
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
            "workstream_key",
            "source_message_ids",
        ],
        "additionalProperties": False,
    }


def _context_request_schema() -> dict[str, object]:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "request_type": {"type": "string"},
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


def anchor_batch_output_schema() -> dict[str, object]:
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
                                "anchor_status": {"type": "string"},
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
                                            "self_relations": _self_relations_schema(),
                                            "workstream_key": {"type": "string"},
                                            "source_message_ids": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
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
                                            "workstream_key",
                                            "source_message_ids",
                                        ],
                                        "additionalProperties": False,
                                    },
                                },
                                "context_requests": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "request_type": {"type": "string"},
                                            "target_message_ids": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                            "target_attachment_ids": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                        },
                                        "required": [
                                            "request_type",
                                            "target_message_ids",
                                            "target_attachment_ids",
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


def _self_relations_schema() -> dict[str, object]:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "relation": {"type": "string"},
                "evidence_message_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["relation", "evidence_message_ids"],
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
                        "group_id": {"type": "string"},
                        "draft_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "primary_draft_id": {"type": "string"},
                    },
                    "required": ["group_id", "draft_ids", "primary_draft_id"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["groups"],
        "additionalProperties": False,
    }


def workstream_assignment_output_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "assignments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "draft_id": {"type": "string"},
                        "parent_draft_id": {"type": "string"},
                        "root_workstream_name": {"type": "string"},
                        "evidence_message_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": [
                        "draft_id",
                        "parent_draft_id",
                        "root_workstream_name",
                        "evidence_message_ids",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["assignments"],
        "additionalProperties": False,
    }


def collected_grouping_output_schema() -> dict[str, object]:
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
                        "summary_title": {"type": "string"},
                        "summary_content": {"type": "string"},
                        "summary_object_hint": {"type": "string"},
                        "group_reason": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": [
                                    "shared_message",
                                    "shared_file",
                                    "same_conversation",
                                    "same_object",
                                    "continuous_action",
                                ],
                            },
                        },
                        "risk_flags": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": [
                                    "cross_batch",
                                    "workstream_conflict",
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
        "required": ["groups"],
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
