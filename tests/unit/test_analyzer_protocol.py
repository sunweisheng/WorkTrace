from __future__ import annotations

from src.worktrace.analyzers.protocol import (
    parse_anchor_analysis_payload,
    parse_batch_analysis_payload,
    parse_collected_grouping_payload,
    parse_merge_payload,
)
from src.worktrace.analyzers.output_schemas import collected_grouping_output_schema


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


def test_collected_grouping_protocol_carries_candidate_summary() -> None:
    payload = {
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
    assert item_schema["required"] == [
        "group_id",
        "draft_ids",
        "summary_title",
        "summary_content",
        "summary_object_hint",
    ]


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
