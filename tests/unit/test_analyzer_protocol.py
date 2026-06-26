from __future__ import annotations

from src.worktrace.analyzers.protocol import (
    parse_anchor_analysis_payload,
    parse_batch_analysis_payload,
    parse_merge_payload,
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
