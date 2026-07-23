from __future__ import annotations

from pathlib import Path

from src.worktrace.analyzers.function_calls import (
    collected_grouping_call_contract,
    task_function_call_spec,
)
from src.worktrace.analyzers.output_schemas import (
    anchor_batch_output_schema,
    batch_output_schema,
    collected_grouping_function_schema,
    collected_merge_output_schema,
    conversation_segmentation_output_schema,
    merge_output_schema,
    personal_fact_review_output_schema,
    retention_review_output_schema,
    segment_batch_output_schema,
    workstream_assignment_output_schema,
)
from src.worktrace.config import RuntimeConfig, load_runtime_config_overrides
from src.worktrace.models import (
    CollectedSourceEvent,
    PersonalFactReviewBatch,
    PersonalFactReviewCandidate,
    SourceBackedEventDraft,
    WorkEvent,
)


CONFIG = load_runtime_config_overrides(RuntimeConfig(), cwd=Path.cwd())


def _assert_strict_objects(schema: object) -> None:
    if isinstance(schema, list):
        for item in schema:
            _assert_strict_objects(item)
        return
    if not isinstance(schema, dict):
        return
    if schema.get("type") == "object":
        properties = schema.get("properties")
        assert isinstance(properties, dict)
        assert schema.get("additionalProperties") is False
        assert set(schema.get("required", [])) == set(properties)
    for value in schema.values():
        _assert_strict_objects(value)


def _personal_fact_batch() -> PersonalFactReviewBatch:
    candidate = SourceBackedEventDraft(
        draft_id="draft-1",
        date="2026-07-23",
        topic="事项",
        content="处理事项。",
        source_message_ids=["m1"],
        source_conversation_id="oc_1",
        source_slice_id="slice-1",
        confidence=0.9,
    )
    return PersonalFactReviewBatch(
        target_date="2026-07-23",
        batch_id="fact-001",
        candidates=[
            PersonalFactReviewCandidate(
                candidate=candidate,
                allowed_evidence_message_ids=["m1"],
            )
        ],
    )


def test_all_production_function_schemas_use_strict_object_shapes() -> None:
    schemas = [
        batch_output_schema(CONFIG),
        conversation_segmentation_output_schema(),
        segment_batch_output_schema(CONFIG),
        retention_review_output_schema(CONFIG),
        personal_fact_review_output_schema(_personal_fact_batch()),
        anchor_batch_output_schema(CONFIG),
        merge_output_schema(),
        workstream_assignment_output_schema(),
        collected_grouping_function_schema(
            CONFIG,
            draft_ids=["d1", "d2"],
            evidence_relation_ids=["MSG-001"],
            include_split_reason=True,
        ),
        collected_merge_output_schema(),
    ]

    for schema in schemas:
        _assert_strict_objects(schema)


def test_task_function_spec_applies_dynamic_enums_and_empty_array_limits() -> None:
    schema = {
        "type": "object",
        "properties": {
            "draft_id": {"type": "string"},
            "draft_ids": {"type": "array", "items": {"type": "string"}},
            "segment_id": {"type": "string"},
            "segment_start_message_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "source_message_ids": {
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
            "workflow_id": {"type": "string"},
        },
        "required": [
            "draft_id",
            "draft_ids",
            "segment_id",
            "segment_start_message_ids",
            "source_message_ids",
            "target_attachment_ids",
            "target_link_ids",
            "workflow_id",
        ],
        "additionalProperties": False,
    }
    spec = task_function_call_spec(
        "preflight",
        schema,
        draft_ids=["d1", "d2"],
        segment_ids=["segment-1"],
        message_ids=["m001", "m002"],
        attachment_ids=[],
        link_ids=[],
        workflow_ids=["workflow-1"],
    )
    properties = spec.parameters["properties"]

    assert properties["draft_id"]["enum"] == ["d1", "d2"]
    assert properties["draft_ids"]["items"]["enum"] == ["d1", "d2"]
    assert properties["segment_id"]["enum"] == ["segment-1"]
    assert properties["segment_start_message_ids"]["items"]["enum"] == [
        "m001",
        "m002",
    ]
    assert properties["source_message_ids"]["items"]["enum"] == ["m001", "m002"]
    assert properties["target_attachment_ids"]["maxItems"] == 0
    assert properties["target_link_ids"]["maxItems"] == 0
    assert properties["workflow_id"]["enum"] == ["workflow-1"]


def _collected_event(
    draft_id: str,
    *,
    messages: list[str] | None = None,
    files: list[str] | None = None,
) -> CollectedSourceEvent:
    return CollectedSourceEvent(
        draft_id=draft_id,
        person_name=draft_id,
        source_file=f"{draft_id}.md",
        event=WorkEvent(
            date="2026-07-23",
            event_id=f"event-{draft_id}",
            title=f"事项 {draft_id}",
            content=f"处理事项 {draft_id}。",
            object_hint=f"事项 {draft_id}",
            retention_reason="decision_made",
            retention_detail=f"形成事项 {draft_id} 的结果。",
            evidence_fingerprints=messages or [],
            file_keys=files or [],
        ),
    )


def test_collected_grouping_contract_numbers_only_real_relations() -> None:
    message = "sha256:" + "a" * 64
    file_key = "sha256:" + "b" * 64
    events = [
        _collected_event("d1", messages=[message], files=[file_key]),
        _collected_event("d2", messages=[message], files=[file_key]),
        _collected_event("d3"),
    ]
    contract = collected_grouping_call_contract(
        "collected_candidate_grouping",
        config=CONFIG,
        events=events,
        deterministic_groups=[],
        include_split_reason=False,
    )
    relation_schema = contract.function_spec.parameters["properties"][
        "merged_groups"
    ]["items"]["properties"]["evidence_relation_ids"]

    assert [item.to_dict() for item in contract.evidence_catalog] == [
        {
            "relation_id": "MSG-001",
            "relation_type": "shared_message",
            "draft_ids": ["d1", "d2"],
            "shared_count": 1,
        },
        {
            "relation_id": "FILE-001",
            "relation_type": "shared_file",
            "draft_ids": ["d1", "d2"],
            "shared_count": 1,
        },
    ]
    assert relation_schema["items"]["enum"] == ["MSG-001", "FILE-001"]
    assert relation_schema["maxItems"] == 2


def test_collected_grouping_contract_disallows_evidence_when_catalog_is_empty() -> None:
    contract = collected_grouping_call_contract(
        "collected_candidate_grouping",
        config=CONFIG,
        events=[_collected_event("d1"), _collected_event("d2")],
        deterministic_groups=[],
        include_split_reason=False,
    )
    relation_schema = contract.function_spec.parameters["properties"][
        "merged_groups"
    ]["items"]["properties"]["evidence_relation_ids"]

    assert contract.evidence_catalog == ()
    assert relation_schema["maxItems"] == 0
    assert "enum" not in relation_schema["items"]
