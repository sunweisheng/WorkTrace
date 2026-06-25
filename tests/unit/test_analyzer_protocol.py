from __future__ import annotations

from src.worktrace.analyzers.protocol import (
    parse_anchor_analysis_payload,
    parse_batch_analysis_payload,
    parse_cross_bucket_merge_payload,
    parse_cross_merge_bucket_payload,
    parse_merge_payload,
    parse_summary_payload,
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
    bucket_result = parse_cross_merge_bucket_payload(
        {"buckets": [{"bucket_id": "b1", "draft_ids": ["d1"], "reason": "ok"}]}
    )
    cross_bucket_result = parse_cross_bucket_merge_payload(
        {
            "merge_decisions": [
                {
                    "left_bucket_id": "b1",
                    "right_bucket_id": "b2",
                    "should_merge": True,
                    "reason": "same thread",
                }
            ]
        }
    )
    merged = parse_merge_payload([])
    summary = parse_summary_payload({"date": "2026-06-22", "summary_text": "ok"})

    assert anchor.anchor_status == "completed"
    assert batch.candidate_events == []
    assert bucket_result.buckets[0].draft_ids == ["d1"]
    assert cross_bucket_result.merge_decisions[0].should_merge is True
    assert merged == []
    assert summary.summary_text == "ok"


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
