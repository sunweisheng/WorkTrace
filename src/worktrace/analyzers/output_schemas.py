from __future__ import annotations


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
                        "source_message_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["topic", "content", "source_message_ids"],
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
                                            "source_message_ids": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                        },
                                        "required": [
                                            "topic",
                                            "content",
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


def merge_output_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "groups": {"type": "array"},
        },
        "required": ["groups"],
        "additionalProperties": True,
    }
