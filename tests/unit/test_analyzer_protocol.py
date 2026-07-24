from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from src.worktrace.errors import AnalyzerProtocolError
from src.worktrace.analyzers.protocol import (
    parse_anchor_analysis_payload,
    parse_batch_analysis_payload,
    parse_collected_grouping_payload,
    parse_collected_grouping_function_payload,
    parse_merge_payload,
    parse_personal_fact_review_payload,
    parse_retention_review_payload,
)
from src.worktrace.analyzers.collected_evidence import (
    EvidenceRelation,
    derive_group_evidence,
)
from src.worktrace.analyzers.output_schemas import (
    batch_output_schema,
    collected_grouping_output_schema,
    personal_fact_review_output_schema,
    retention_review_output_schema,
)
from src.worktrace.config import RuntimeConfig, load_runtime_config_overrides
from src.worktrace.models import (
    PersonalFactReviewBatch,
    PersonalFactReviewCandidate,
    SourceBackedEventDraft,
)


def _personal_fact_review_batch() -> PersonalFactReviewBatch:
    candidate = SourceBackedEventDraft(
        draft_id="d1",
        date="2026-07-15",
        topic="设备编号修正",
        content="重新修改未生效的设备编号。",
        source_message_ids=["m1"],
        source_conversation_id="oc_1",
        source_slice_id="slice-1",
        confidence=0.9,
    )
    return PersonalFactReviewBatch(
        target_date="2026-07-15",
        batch_id="personal-fact-review-001",
        candidates=[
            PersonalFactReviewCandidate(
                candidate=candidate,
                allowed_evidence_message_ids=["m1"],
            )
        ],
    )


def test_protocol_parsers_accept_valid_payloads() -> None:
    anchor = parse_anchor_analysis_payload(
        {
            "anchor_status": "completed",
            "candidate_events": [],
            "context_requests": [],
            "needs_cross_anchor_merge": False,
        }
    )
    batch = parse_batch_analysis_payload({"candidate_events": [], "context_requests": []})
    merged = parse_merge_payload({"groups": []})

    assert anchor.anchor_status == "completed"
    assert batch.candidate_events == []
    assert merged.groups == []


def test_retention_review_protocol_and_schema_use_configured_signal_types() -> None:
    config = load_runtime_config_overrides(RuntimeConfig(), cwd=Path.cwd())
    payload = {
        "results": [
            {
                "draft_id": "d1",
                "routine_signals": [
                    {
                        "type": "presence_or_availability",
                        "evidence_message_ids": ["m1"],
                    }
                ],
                "substantive_signals": [],
            }
        ]
    }

    parsed = parse_retention_review_payload(payload)
    schema = retention_review_output_schema(config)
    item_schema = schema["properties"]["results"]["items"]
    routine_enum = item_schema["properties"]["routine_signals"]["items"][
        "properties"
    ]["type"]["enum"]

    assert parsed.to_dict() == payload
    assert "presence_or_availability" in routine_enum
    assert "explicit_business_follow_up" not in routine_enum


def test_personal_fact_review_protocol_and_schema_require_source_backed_fields() -> None:
    batch = _personal_fact_review_batch()
    payload = {
        "results": [
            {
                "draft_id": "d1",
                "supported": True,
                "fact_items": {
                    "topic": {
                        "text": "设备编号修正",
                        "evidence_message_ids": ["m1"],
                    },
                    "content": [
                        {
                            "text": "重新修改未生效的设备编号。",
                            "evidence_message_ids": ["m1"],
                        }
                    ],
                    "action_label": {
                        "text": "修改",
                        "evidence_message_ids": ["m1"],
                    },
                    "object_hint": {
                        "text": "设备编号",
                        "evidence_message_ids": ["m1"],
                    },
                    "retention_detail": {
                        "text": "执行人确认重新修改设备编号。",
                        "evidence_message_ids": ["m1"],
                    },
                    "workstream_key": {"text": "", "evidence_message_ids": []},
                },
                "removed_claims": [],
            }
        ]
    }

    parsed = parse_personal_fact_review_payload(payload)
    schema = personal_fact_review_output_schema(batch)
    item_schema = schema["properties"]["results"]["items"]
    evidence_schema = item_schema["properties"]["fact_items"]["properties"][
        "topic"
    ]["properties"]["evidence_message_ids"]["items"]

    assert parsed.results[0].topic == "设备编号修正"
    assert parsed.results[0].content == "重新修改未生效的设备编号。"
    assert parsed.results[0].fact_items[2].field_name == "action_label"
    assert parsed.results[0].fact_items[2].text == "修改"
    assert "topic" not in item_schema["properties"]
    assert "fact_items" in item_schema["required"]
    assert item_schema["properties"]["fact_items"]["type"] == "object"
    assert "action_label" in item_schema["properties"]["fact_items"]["required"]
    assert item_schema["properties"]["draft_id"]["enum"] == ["d1"]
    assert evidence_schema["enum"] == ["m1"]
    assert item_schema["additionalProperties"] is False


def test_personal_fact_review_schema_requires_single_candidate() -> None:
    batch = _personal_fact_review_batch()

    with pytest.raises(ValueError, match="exactly one candidate"):
        personal_fact_review_output_schema(
            replace(batch, candidates=[*batch.candidates, *batch.candidates])
        )


def test_personal_extraction_schema_requires_fact_evidence_and_configured_risks() -> None:
    config = load_runtime_config_overrides(RuntimeConfig(), cwd=Path.cwd())
    schema = batch_output_schema(config)
    item_schema = schema["properties"]["candidate_events"]["items"]
    risk_enum = item_schema["properties"]["fact_risk_flags"]["items"]["enum"]

    assert "fact_items" in item_schema["required"]
    assert "fact_risk_flags" in item_schema["required"]
    assert "comparison_or_example" in risk_enum


def test_personal_fact_review_protocol_rejects_model_keep_drop_field() -> None:
    payload = {
        "results": [
            {
                "draft_id": "d1",
                "supported": True,
                "fact_items": {
                    "topic": {"text": "", "evidence_message_ids": []},
                    "content": [],
                    "action_label": {"text": "", "evidence_message_ids": []},
                    "object_hint": {"text": "", "evidence_message_ids": []},
                    "retention_detail": {"text": "", "evidence_message_ids": []},
                    "workstream_key": {"text": "", "evidence_message_ids": []},
                },
                "removed_claims": [],
                "keep": True,
            }
        ]
    }

    with pytest.raises(AnalyzerProtocolError):
        parse_personal_fact_review_payload(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"results": [{"draft_id": "d1", "routine_signals": []}]},
        {
            "results": [
                {
                    "draft_id": "d1",
                    "routine_signals": {},
                    "substantive_signals": [],
                }
            ]
        },
        {
            "results": [
                {
                    "draft_id": "d1",
                    "routine_signals": [
                        {
                            "type": "presence_or_availability",
                            "evidence_message_ids": "m1",
                        }
                    ],
                    "substantive_signals": [],
                }
            ]
        },
    ],
)
def test_retention_review_protocol_rejects_incomplete_shapes(payload: object) -> None:
    with pytest.raises(AnalyzerProtocolError):
        parse_retention_review_payload(payload)


def test_collected_grouping_protocol_carries_candidate_summary() -> None:
    payload = {
        "split_reason": "价格方案与执行反馈属于不同事项。",
        "groups": [
            {
                "group_id": "g1",
                "draft_ids": ["d1", "d2"],
                "summary_title": "价格方案评估",
                "summary_content": "提出价格方案并反馈执行影响。",
                "summary_object_hint": "价格方案",
            }
        ]
    }

    parsed = parse_collected_grouping_payload(payload)
    schema = collected_grouping_output_schema()
    item_schema = schema["properties"]["groups"]["items"]

    assert parsed.groups[0].summary_content == payload["groups"][0]["summary_content"]
    assert parsed.split_reason == payload["split_reason"]
    assert schema["required"] == ["split_reason", "groups"]
    assert item_schema["required"] == [
        "group_id",
        "draft_ids",
        "summary_title",
        "summary_content",
        "summary_object_hint",
        "group_reason",
        "risk_flags",
    ]
    assert "same_deliverable_batch" in item_schema["properties"]["group_reason"][
        "items"
    ]["enum"]


def test_collected_grouping_protocol_reads_legacy_group_split_reason() -> None:
    parsed = parse_collected_grouping_payload(
        {
            "groups": [
                {
                    "group_id": "g1",
                    "draft_ids": ["d1"],
                    "split_reason": "旧记录中的整体拆分理由。",
                },
                {
                    "group_id": "g2",
                    "draft_ids": ["d2"],
                    "split_reason": "",
                },
            ]
        }
    )

    assert parsed.split_reason == ""
    assert parsed.groups[0].split_reason == "旧记录中的整体拆分理由。"


def test_collected_function_payload_restores_verified_internal_reasons() -> None:
    result, errors = parse_collected_grouping_function_payload(
        {
            "merged_groups": [
                {
                    "group_id": "g1",
                    "draft_ids": ["d1", "d2"],
                    "summary_title": "同一事项",
                    "summary_content": "两条记录描述同一事项。",
                    "summary_object_hint": "同一事项",
                    "semantic_reasons": ["same_object"],
                    "reason_detail": "具体对象和前后动作一致。",
                    "member_connections": [
                        {"draft_id": "d1", "connection_detail": "处理该事项。"},
                        {"draft_id": "d2", "connection_detail": "继续该事项。"},
                    ],
                    "risk_flags": [],
                }
            ],
            "singleton_draft_ids": [],
        },
        evidence_catalog=[
            EvidenceRelation("MSG-001", "shared_message", ("d1", "d2"), 2),
            EvidenceRelation("FILE-001", "shared_file", ("d1", "d2"), 1),
        ],
        allowed_semantic_reasons=["same_object"],
    )

    assert errors == []
    assert result.groups[0].group_reason == [
        "shared_message",
        "same_object",
    ]
    assert result.groups[0].evidence_relation_ids == ["MSG-001"]
    assert result.groups[0].reason_detail == "具体对象和前后动作一致。"


@pytest.mark.parametrize(
    ("relation_ids", "catalog", "expected_error"),
    [
        (
            ["MSG-999"],
            [],
            "unknown_evidence_relation",
        ),
        (
            ["MSG-001"],
            [EvidenceRelation("MSG-001", "shared_message", ("d1", "d4"), 1)],
            "evidence_outside_group",
        ),
        (
            ["MSG-001"],
            [EvidenceRelation("MSG-001", "shared_message", ("d1", "d2"), 1)],
            "evidence_does_not_cover_group",
        ),
    ],
)
def test_collected_function_payload_rejects_invalid_evidence_relations(
    relation_ids: list[str],
    catalog: list[EvidenceRelation],
    expected_error: str,
) -> None:
    result, errors = parse_collected_grouping_function_payload(
        {
            "merged_groups": [
                {
                    "group_id": "g1",
                    "draft_ids": ["d1", "d2", "d3"],
                    "summary_title": "候选事项",
                    "summary_content": "候选事项记录。",
                    "summary_object_hint": "候选事项",
                    "semantic_reasons": [],
                    "evidence_relation_ids": relation_ids,
                    "reason_detail": "依据共同证据尝试合并。",
                    "risk_flags": [],
                }
            ],
            "singleton_draft_ids": [],
        },
            evidence_catalog=catalog,
            allow_model_evidence_relation_ids=True,
            require_member_connections=False,
    )

    assert any(error.startswith(expected_error) for error in errors)
    assert result.validation_errors == errors
    matching_error = next(error for error in errors if error.startswith(expected_error))
    if expected_error == "evidence_outside_group":
        assert "relation_draft_ids=['d1', 'd4']" in matching_error
        assert "group_draft_ids=['d1', 'd2', 'd3']" in matching_error
    if expected_error == "evidence_does_not_cover_group":
        assert "relation_endpoints=[['d1', 'd2']]" in matching_error


def test_collected_function_payload_requires_reason_detail_for_multi_group() -> None:
    _result, errors = parse_collected_grouping_function_payload(
        {
            "split_reason": "第三条记录属于不同对象。",
            "merged_groups": [
                {
                    "group_id": "g1",
                    "draft_ids": ["d1", "d2"],
                    "summary_title": "同一事项",
                    "summary_content": "两条记录描述同一事项。",
                    "summary_object_hint": "同一事项",
                    "semantic_reasons": ["same_object"],
                    "reason_detail": "",
                    "member_connections": [
                        {"draft_id": "d1", "connection_detail": "处理该事项。"},
                        {"draft_id": "d2", "connection_detail": "继续该事项。"},
                    ],
                    "risk_flags": [],
                }
            ],
            "singleton_draft_ids": ["d3"],
        },
        evidence_catalog=[],
        allowed_semantic_reasons=["same_object"],
    )

    assert any(error.startswith("reason_detail_missing") for error in errors)


def test_collected_function_payload_requires_exact_member_connections() -> None:
    _result, errors = parse_collected_grouping_function_payload(
        {
            "merged_groups": [
                {
                    "group_id": "g1",
                    "draft_ids": ["d1", "d2"],
                    "summary_title": "同一事项",
                    "summary_content": "两条记录描述同一事项。",
                    "summary_object_hint": "同一事项",
                    "semantic_reasons": ["same_object"],
                    "reason_detail": "具体对象一致。",
                    "member_connections": [
                        {"draft_id": "d1", "connection_detail": "处理对象。"},
                        {"draft_id": "d1", "connection_detail": "重复说明。"},
                        {"draft_id": "d3", "connection_detail": "组外说明。"},
                    ],
                    "risk_flags": [],
                }
            ],
            "singleton_draft_ids": [],
        },
        evidence_catalog=[],
        allowed_semantic_reasons=["same_object"],
    )

    assert any(error.startswith("duplicate_member_connection") for error in errors)
    assert any(error.startswith("unknown_member_connection") for error in errors)
    assert any(error.startswith("missing_member_connection") for error in errors)


def test_python_derives_stable_mixed_evidence_spanning_set() -> None:
    audit = derive_group_evidence(
        ["d1", "d2", "d3"],
        [
            EvidenceRelation("MSG-001", "shared_message", ("d1", "d2"), 1),
            EvidenceRelation("FILE-001", "shared_file", ("d2", "d3"), 1),
            EvidenceRelation("MSG-002", "shared_message", ("d1", "d3"), 1),
        ],
    )

    assert audit.connected is True
    assert audit.basis_relation_ids == ("MSG-001", "FILE-001")
    assert audit.uncovered_draft_ids == ()


def test_python_prefers_minimum_evidence_set_before_catalog_order() -> None:
    audit = derive_group_evidence(
        ["d1", "d2", "d3"],
        [
            EvidenceRelation("MSG-001", "shared_message", ("d1", "d2"), 1),
            EvidenceRelation("FILE-001", "shared_file", ("d2", "d3"), 1),
            EvidenceRelation(
                "MSG-002",
                "shared_message",
                ("d1", "d2", "d3"),
                1,
            ),
        ],
    )

    assert audit.connected is True
    assert audit.basis_relation_ids == ("MSG-002",)


def test_python_keeps_partial_evidence_out_of_merge_basis() -> None:
    audit = derive_group_evidence(
        ["d1", "d2", "d3"],
        [EvidenceRelation("MSG-001", "shared_message", ("d1", "d2"), 1)],
    )

    assert audit.connected is False
    assert audit.contained_relation_ids == ("MSG-001",)
    assert audit.basis_relation_ids == ()
    assert audit.uncovered_draft_ids == ("d3",)


def test_anchor_status_list_string_is_normalized_to_single_status() -> None:
    anchor = parse_anchor_analysis_payload(
        {
            "anchor_status": "['completed', 'needs_more_context']",
            "candidate_events": [],
            "context_requests": [],
            "needs_cross_anchor_merge": False,
        }
    )

    assert anchor.anchor_status == "needs_more_context"


def test_anchor_status_list_payload_is_normalized_to_single_status() -> None:
    anchor = parse_anchor_analysis_payload(
        {
            "anchor_status": ["completed", "needs_attachment_text"],
            "candidate_events": [],
            "context_requests": [],
            "needs_cross_anchor_merge": False,
        }
    )

    assert anchor.anchor_status == "needs_attachment_text"


def test_batch_parser_drops_invalid_context_request_items() -> None:
    batch = parse_batch_analysis_payload(
        {
            "candidate_events": [],
            "context_requests": [
                {
                    "slice_id": "slice-1",
                    "request_type": "later_messages",
                    "target_message_ids": ["om_1"],
                    "target_attachment_ids": [],
                    "target_link_ids": [],
                    "reason": "need more context",
                    "limit": 1,
                },
                {
                    "request_type": "attachment_text",
                    "target_attachment_ids": ["att_1"],
                },
            ],
        }
    )

    assert len(batch.context_requests) == 1
    assert batch.context_requests[0].slice_id == "slice-1"


def test_batch_parser_accepts_linked_file_text_request() -> None:
    batch = parse_batch_analysis_payload(
        {
            "candidate_events": [],
            "context_requests": [
                {
                    "request_type": "linked_file_text",
                    "target_message_ids": ["om_1"],
                    "target_attachment_ids": [],
                    "target_link_ids": ["om_1#link1"],
                    "limit": 1,
                }
            ],
        }
    )

    assert batch.context_requests[0].request_type == "linked_file_text"
    assert batch.context_requests[0].target_link_ids == ["om_1#link1"]


def test_batch_parser_accepts_minimal_payload_shape() -> None:
    batch = parse_batch_analysis_payload(
        {
            "candidate_events": [
                {
                    "topic": "发布推进",
                    "content": "推进发布并确认上线窗口",
                    "source_message_ids": ["om_1"],
                }
            ],
            "context_requests": [
                {
                    "request_type": "later_messages",
                    "target_message_ids": ["om_1"],
                    "target_attachment_ids": [],
                    "target_link_ids": [],
                }
            ],
        }
    )

    assert batch.candidate_events[0].topic == "发布推进"
    assert batch.context_requests[0].limit == 1
