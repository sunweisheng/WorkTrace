from src.worktrace.models import (
    SourceBackedEventDraft,
    WorkstreamAssignment,
    WorkstreamAssignmentResult,
)
from src.worktrace.pipeline.workstream_resolution import (
    groups_from_workstream_assignments,
)


def _draft(
    draft_id: str,
    *,
    workstream_key: str = "",
    message_id: str,
) -> SourceBackedEventDraft:
    return SourceBackedEventDraft(
        draft_id=draft_id,
        date="2026-07-10",
        topic=draft_id,
        content=draft_id,
        source_message_ids=[message_id],
        source_conversation_id="oc_1",
        source_slice_id="slice-1",
        confidence=0.9,
        object_hint=draft_id,
        workstream_key=workstream_key,
    )


def test_llm_assignments_merge_project_branches_and_policy_feedback() -> None:
    project_root = _draft("project-root", workstream_key="项目甲", message_id="om_1")
    camera_task = _draft("camera-task", message_id="om_2")
    policy_root = _draft("policy-root", workstream_key="政策乙", message_id="om_3")
    policy_feedback = _draft("policy-feedback", message_id="om_4")
    unrelated = _draft("unrelated", message_id="om_5")

    groups, warnings = groups_from_workstream_assignments(
        WorkstreamAssignmentResult(
            assignments=[
                WorkstreamAssignment("project-root", "project-root", "项目甲"),
                WorkstreamAssignment(
                    "camera-task",
                    "project-root",
                    evidence_message_ids=["om_1", "om_2"],
                ),
                WorkstreamAssignment("policy-root", "policy-root", "政策乙"),
                WorkstreamAssignment(
                    "policy-feedback",
                    "policy-root",
                    evidence_message_ids=["om_3", "om_4"],
                ),
                WorkstreamAssignment("unrelated", ""),
            ]
        ),
        [project_root, camera_task, policy_root, policy_feedback, unrelated],
    )

    grouped_ids = {frozenset(group.draft_ids): group.primary_draft_id for group in groups}

    assert grouped_ids[frozenset({"project-root", "camera-task"})] == "project-root"
    assert grouped_ids[frozenset({"policy-root", "policy-feedback"})] == "policy-root"
    assert frozenset({"unrelated"}) in grouped_ids
    assert warnings == []


def test_llm_assignment_without_source_evidence_stays_independent() -> None:
    project_root = _draft("project-root", workstream_key="项目甲", message_id="om_1")
    task = _draft("task", message_id="om_2")

    groups, warnings = groups_from_workstream_assignments(
        WorkstreamAssignmentResult(
            assignments=[
                WorkstreamAssignment("project-root", "project-root", "项目甲"),
                WorkstreamAssignment(
                    "task",
                    "project-root",
                    evidence_message_ids=["om_unknown"],
                ),
            ]
        ),
        [project_root, task],
    )

    assert [group.draft_ids for group in groups] == [["project-root"], ["task"]]
    assert warnings == ["Ignored workstream assignment without source evidence: task."]


def test_llm_assignments_resolve_a_named_parent_chain() -> None:
    project_root = _draft("project-root", message_id="om_1")
    project_start = _draft("project-start", workstream_key="项目甲", message_id="om_2")
    camera_task = _draft("camera-task", message_id="om_3")

    groups, warnings = groups_from_workstream_assignments(
        WorkstreamAssignmentResult(
            assignments=[
                WorkstreamAssignment("project-root", "project-root", "项目甲"),
                WorkstreamAssignment(
                    "project-start",
                    "project-root",
                    evidence_message_ids=["om_1", "om_2"],
                ),
                WorkstreamAssignment(
                    "camera-task",
                    "project-start",
                    evidence_message_ids=["om_2", "om_3"],
                ),
            ]
        ),
        [project_root, project_start, camera_task],
    )

    assert [group.draft_ids for group in groups] == [
        ["project-root", "project-start", "camera-task"]
    ]
    assert groups[0].primary_draft_id == "project-root"
    assert warnings == []
