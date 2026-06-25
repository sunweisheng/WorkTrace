from __future__ import annotations


def batch_output_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "candidate_events": {"type": "array"},
            "context_requests": {"type": "array"},
        },
        "required": ["candidate_events", "context_requests"],
        "additionalProperties": True,
    }


def anchor_batch_output_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "results": {"type": "array"},
        },
        "required": ["results"],
        "additionalProperties": True,
    }


def merge_output_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "groups": {"type": "array"},
        },
        "required": ["groups"],
        "additionalProperties": True,
    }


def bucket_output_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "buckets": {"type": "array"},
        },
        "required": ["buckets"],
        "additionalProperties": True,
    }


def cross_bucket_merge_output_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "merge_decisions": {"type": "array"},
        },
        "required": ["merge_decisions"],
        "additionalProperties": True,
    }

