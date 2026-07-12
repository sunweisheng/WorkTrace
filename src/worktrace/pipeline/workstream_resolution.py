from __future__ import annotations

from ..models import (
    CrossConversationGroup,
    SourceBackedEventDraft,
    WorkstreamAssignmentResult,
)


def groups_from_workstream_assignments(
    result: WorkstreamAssignmentResult,
    candidates: list[SourceBackedEventDraft],
) -> tuple[list[CrossConversationGroup], list[str]]:
    candidate_by_id = {candidate.draft_id: candidate for candidate in candidates}
    assignments_by_id = {}
    warnings: list[str] = []

    for assignment in result.assignments:
        if assignment.draft_id not in candidate_by_id:
            warnings.append(
                f"Ignored workstream assignment with unknown draft: {assignment.draft_id}."
            )
            continue
        if assignment.draft_id in assignments_by_id:
            warnings.append(
                f"Ignored duplicate workstream assignment: {assignment.draft_id}."
            )
            continue
        assignments_by_id[assignment.draft_id] = assignment

    root_ids = [
        candidate.draft_id
        for candidate in candidates
        if (
            assignments_by_id.get(candidate.draft_id) is not None
            and assignments_by_id[candidate.draft_id].parent_draft_id == candidate.draft_id
            and assignments_by_id[candidate.draft_id].root_workstream_name.strip()
        )
    ]
    parent_by_id: dict[str, str] = {}

    for candidate in candidates:
        draft_id = candidate.draft_id
        assignment = assignments_by_id.get(draft_id)
        if assignment is None or not assignment.parent_draft_id:
            continue
        if assignment.parent_draft_id == draft_id:
            if draft_id not in root_ids:
                warnings.append(
                    f"Ignored unnamed workstream root: {draft_id}."
                )
            continue

        parent_id = assignment.parent_draft_id
        parent = candidate_by_id.get(parent_id)
        if parent is None:
            warnings.append(
                f"Ignored assignment to an unknown workstream parent: {draft_id}."
            )
            continue
        if not _has_assignment_evidence(
            assignment.evidence_message_ids,
            child=candidate,
            parent=parent,
        ):
            warnings.append(
                f"Ignored workstream assignment without source evidence: {draft_id}."
            )
            continue
        parent_by_id[draft_id] = parent_id

    root_by_id = {
        candidate.draft_id: _resolve_root_id(
            candidate.draft_id,
            parent_by_id=parent_by_id,
            root_ids=set(root_ids),
        )
        for candidate in candidates
    }
    grouped_ids: dict[str, list[str]] = {root_id: [] for root_id in root_ids}
    standalone_ids: list[str] = []
    for candidate in candidates:
        root_id = root_by_id[candidate.draft_id]
        if root_id:
            grouped_ids[root_id].append(candidate.draft_id)
        else:
            standalone_ids.append(candidate.draft_id)

    groups = [
        CrossConversationGroup(
            group_id=f"workstream-{root_id}",
            draft_ids=draft_ids,
            primary_draft_id=root_id,
        )
        for root_id, draft_ids in grouped_ids.items()
    ]
    groups.extend(
        CrossConversationGroup(
            group_id=f"standalone-{draft_id}",
            draft_ids=[draft_id],
            primary_draft_id=draft_id,
        )
        for draft_id in standalone_ids
    )
    return groups, warnings


def _has_assignment_evidence(
    evidence_message_ids: list[str],
    *,
    child: SourceBackedEventDraft,
    parent: SourceBackedEventDraft,
) -> bool:
    evidence_ids = set(evidence_message_ids)
    supported_ids = set(child.source_message_ids) | set(parent.source_message_ids)
    return (
        bool(evidence_ids)
        and evidence_ids.issubset(supported_ids)
    )


def _resolve_root_id(
    draft_id: str,
    *,
    parent_by_id: dict[str, str],
    root_ids: set[str],
) -> str:
    current_id = draft_id
    visited: set[str] = set()
    while current_id not in visited:
        if current_id in root_ids:
            return current_id
        visited.add(current_id)
        parent_id = parent_by_id.get(current_id)
        if not parent_id:
            return ""
        current_id = parent_id
    return ""
