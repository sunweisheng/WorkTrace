from __future__ import annotations

from pathlib import Path

from src.worktrace.analyzers.function_calls import (
    collected_grouping_call_contract,
    personal_grouping_call_contract,
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
        collected_grouping_function_schema(
            CONFIG,
            draft_ids=["d1", "d2"],
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
        },
        "required": [
            "draft_id",
            "draft_ids",
            "segment_id",
            "segment_start_message_ids",
            "source_message_ids",
            "target_attachment_ids",
            "target_link_ids",
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


def test_day_grouping_function_has_only_grouping_fields() -> None:
    schema = merge_output_schema()
    group = schema["properties"]["groups"]["items"]

    assert set(group["properties"]) == {
        "draft_ids",
        "primary_draft_id",
        "merge_reason",
        "evidence_message_ids",
    }
    assert "group_id" not in group["properties"]


def test_personal_grouping_contract_defaults_to_singletons_and_requires_connections() -> None:
    candidates = [
        SourceBackedEventDraft(
            draft_id="d1",
            date="2026-07-22",
            topic="事项一",
            content="处理事项一。",
            source_message_ids=["m1"],
            source_conversation_id="oc1",
            source_slice_id="s1",
            confidence=0.9,
        ),
        SourceBackedEventDraft(
            draft_id="d2",
            date="2026-07-22",
            topic="事项二",
            content="处理事项二。",
            source_message_ids=["m2"],
            source_conversation_id="oc2",
            source_slice_id="s2",
            confidence=0.9,
        ),
    ]

    contract = personal_grouping_call_contract(
        config=CONFIG,
        candidates=candidates,
    )
    properties = contract.function_spec.parameters["properties"]
    group = properties["merged_groups"]["items"]
    connection = group["properties"]["member_connections"]["items"]

    assert contract.function_spec.typical_arguments == {
        "merged_groups": [],
        "singleton_draft_ids": ["d1", "d2"],
    }
    assert group["properties"]["semantic_reasons"]["items"]["enum"] == [
        "same_object",
        "continuous_action",
        "same_deliverable_batch",
    ]
    assert connection["properties"]["evidence_message_ids"]["items"]["enum"] == [
        "m1",
        "m2",
    ]


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
    group_properties = contract.function_spec.parameters["properties"][
        "merged_groups"
    ]["items"]["properties"]

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
    assert "evidence_relation_ids" not in group_properties
    assert group_properties["member_connections"]["items"]["properties"][
        "draft_id"
    ]["enum"] == ["d1", "d2", "d3"]


def test_collected_grouping_contract_keeps_evidence_out_of_model_output() -> None:
    contract = collected_grouping_call_contract(
        "collected_candidate_grouping",
        config=CONFIG,
        events=[_collected_event("d1"), _collected_event("d2")],
        deterministic_groups=[],
        include_split_reason=False,
    )
    group_properties = contract.function_spec.parameters["properties"][
        "merged_groups"
    ]["items"]["properties"]

    assert contract.evidence_catalog == ()
    assert "evidence_relation_ids" not in group_properties
    assert "member_connections" in group_properties
    structure_example = contract.function_spec.argument_structure_example
    assert structure_example is not None
    merged_group = structure_example["merged_group"]
    assert merged_group["draft_ids"] == [
        "<input_draft_id_1>",
        "<input_draft_id_2>",
    ]
    assert [
        item["draft_id"] for item in merged_group["member_connections"]
    ] == ["<input_draft_id_1>", "<input_draft_id_2>"]
    assert "d1" not in str(structure_example)
    assert "d2" not in str(structure_example)


def test_collected_review_example_does_not_preselect_same_object_group() -> None:
    events = [_collected_event("d1"), _collected_event("d2")]
    contract = collected_grouping_call_contract(
        "collected_group_review",
        config=CONFIG,
        events=events,
        deterministic_groups=[["d1", "d2"]],
        include_split_reason=True,
    )

    assert contract.function_spec.typical_arguments["merged_groups"] == []
    assert contract.function_spec.typical_arguments["singleton_draft_ids"] == [
        "d1",
        "d2",
    ]
    assert contract.function_spec.typical_arguments["split_reason"]
