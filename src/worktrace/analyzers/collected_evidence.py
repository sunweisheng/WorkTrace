from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from itertools import combinations


@dataclass(frozen=True)
class EvidenceRelation:
    relation_id: str
    relation_type: str
    draft_ids: tuple[str, ...]
    shared_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "relation_id": self.relation_id,
            "relation_type": self.relation_type,
            "draft_ids": list(self.draft_ids),
            "shared_count": self.shared_count,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "EvidenceRelation":
        return cls(
            relation_id=str(payload.get("relation_id", "")),
            relation_type=str(payload.get("relation_type", "")),
            draft_ids=tuple(str(value) for value in payload.get("draft_ids", [])),
            shared_count=int(payload.get("shared_count", 0) or 0),
        )


@dataclass(frozen=True)
class GroupEvidenceAudit:
    contained_relation_ids: tuple[str, ...]
    selected_relation_ids: tuple[str, ...]
    basis_relation_ids: tuple[str, ...]
    connected: bool
    covered_draft_ids: tuple[str, ...]
    uncovered_draft_ids: tuple[str, ...]
    connected_components: tuple[tuple[str, ...], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "contained_relation_ids": list(self.contained_relation_ids),
            "selected_relation_ids": list(self.selected_relation_ids),
            "basis_relation_ids": list(self.basis_relation_ids),
            "connected": self.connected,
            "covered_draft_ids": list(self.covered_draft_ids),
            "uncovered_draft_ids": list(self.uncovered_draft_ids),
            "connected_components": [list(item) for item in self.connected_components],
        }


def build_evidence_relation_catalog(
    evidence_relations: list[dict[str, object]],
) -> list[EvidenceRelation]:
    catalog: list[EvidenceRelation] = []
    counters = {"shared_message": 0, "shared_file": 0}
    for relation in evidence_relations:
        draft_ids = tuple(
            str(value)
            for value in relation.get("draft_ids", [])
            if str(value).strip()
        )
        for count_field, relation_type, prefix in (
            ("shared_message_count", "shared_message", "MSG"),
            ("shared_file_count", "shared_file", "FILE"),
        ):
            count = int(relation.get(count_field, 0) or 0)
            if count <= 0:
                continue
            counters[relation_type] += 1
            catalog.append(
                EvidenceRelation(
                    relation_id=f"{prefix}-{counters[relation_type]:03d}",
                    relation_type=relation_type,
                    draft_ids=draft_ids,
                    shared_count=count,
                )
            )
    return catalog


def selected_relations_cover_group(
    draft_ids: list[str],
    relations: list[EvidenceRelation],
) -> bool:
    if len(draft_ids) <= 1:
        return True
    members = set(draft_ids)
    adjacent = {draft_id: set() for draft_id in draft_ids}
    for relation in relations:
        relation_members = members.intersection(relation.draft_ids)
        for left, right in combinations(relation_members, 2):
            adjacent[left].add(right)
            adjacent[right].add(left)

    visited: set[str] = set()
    pending = [draft_ids[0]]
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        pending.extend(adjacent[current].difference(visited))
    return visited == members


def derive_group_evidence(
    draft_ids: list[str],
    relations: list[EvidenceRelation],
) -> GroupEvidenceAudit:
    members = list(dict.fromkeys(draft_ids))
    member_set = set(members)
    contained = [
        relation
        for relation in relations
        if len(set(relation.draft_ids)) >= 2
        and set(relation.draft_ids).issubset(member_set)
    ]
    if len(members) <= 1:
        return GroupEvidenceAudit(
            contained_relation_ids=tuple(item.relation_id for item in contained),
            selected_relation_ids=(),
            basis_relation_ids=(),
            connected=True,
            covered_draft_ids=tuple(members),
            uncovered_draft_ids=(),
            connected_components=(tuple(members),) if members else (),
        )

    member_indexes = {draft_id: index for index, draft_id in enumerate(members)}

    def normalize_partition(labels: tuple[int, ...]) -> tuple[int, ...]:
        normalized: dict[int, int] = {}
        return tuple(
            normalized.setdefault(label, len(normalized)) for label in labels
        )

    def merge_relation(
        labels: tuple[int, ...],
        relation: EvidenceRelation,
    ) -> tuple[int, ...]:
        indexes = [
            member_indexes[draft_id]
            for draft_id in relation.draft_ids
            if draft_id in member_indexes
        ]
        if len(indexes) < 2:
            return labels
        merged_labels = set(labels[index] for index in indexes)
        target_label = min(merged_labels)
        return normalize_partition(
            tuple(
                target_label if label in merged_labels else label
                for label in labels
            )
        )

    initial_partition = tuple(range(len(members)))
    states: dict[tuple[int, ...], tuple[int, ...]] = {initial_partition: ()}
    for relation_index, relation in enumerate(contained):
        next_states = dict(states)
        for state, path in states.items():
            merged_state = merge_relation(state, relation)
            if merged_state == state:
                continue
            candidate_path = (*path, relation_index)
            current_path = next_states.get(merged_state)
            if current_path is None or (len(candidate_path), candidate_path) < (
                len(current_path),
                current_path,
            ):
                next_states[merged_state] = candidate_path
        states = next_states

    connected_partition = tuple(0 for _ in members)
    minimum_path = states.get(connected_partition)

    parents = {draft_id: draft_id for draft_id in members}

    def find(draft_id: str) -> str:
        while parents[draft_id] != draft_id:
            parents[draft_id] = parents[parents[draft_id]]
            draft_id = parents[draft_id]
        return draft_id

    def merge(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    forest_relations: list[EvidenceRelation] = []
    for relation in contained:
        relation_members = [
            draft_id for draft_id in members if draft_id in set(relation.draft_ids)
        ]
        roots = {find(draft_id) for draft_id in relation_members}
        if len(roots) <= 1:
            continue
        forest_relations.append(relation)
        first = relation_members[0]
        for draft_id in relation_members[1:]:
            merge(first, draft_id)

    components_by_root: dict[str, list[str]] = {}
    for draft_id in members:
        components_by_root.setdefault(find(draft_id), []).append(draft_id)
    components = tuple(tuple(values) for values in components_by_root.values())
    connected = len(components) == 1
    covered = max(components, key=len, default=())
    uncovered = tuple(draft_id for draft_id in members if draft_id not in covered)
    selected = (
        [contained[index] for index in minimum_path]
        if minimum_path is not None
        else forest_relations
    )
    selected_ids = tuple(item.relation_id for item in selected)
    return GroupEvidenceAudit(
        contained_relation_ids=tuple(item.relation_id for item in contained),
        selected_relation_ids=selected_ids,
        basis_relation_ids=selected_ids if connected else (),
        connected=connected,
        covered_draft_ids=covered,
        uncovered_draft_ids=uncovered,
        connected_components=components,
    )


def derive_semantic_review_trigger_reasons(
    *,
    group: Any,
    source_events: list[Any],
    config: Any,
    relations: list[EvidenceRelation],
) -> tuple[str, ...]:
    reasons: list[str] = []
    draft_ids = [str(value) for value in getattr(group, "draft_ids", [])]
    evidence_audit = derive_group_evidence(draft_ids, relations)
    normalized_objects = {
        "".join(str(getattr(item.event, "object_hint", "")).casefold().split())
        for item in source_events
        if str(getattr(item.event, "object_hint", "")).strip()
    }
    supported_semantic_reasons = {
        str(getattr(item, "key", ""))
        for item in getattr(config, "collected_group_reason_definitions", ())
        if bool(getattr(item, "supports_semantic_merge", False))
    }
    group_reasons = [
        *getattr(group, "group_reason", []),
        *getattr(group, "semantic_reasons", []),
    ]
    if (
        bool(getattr(config, "review_semantic_only_object_conflicts", False))
        and supported_semantic_reasons.intersection(group_reasons)
        and not evidence_audit.connected
        and len(normalized_objects) > 1
    ):
        reasons.append("semantic_only_object_conflict")
    if (
        bool(getattr(config, "review_broad_object_groups", False))
        and "broad_object" in getattr(group, "risk_flags", [])
    ):
        reasons.append("broad_object")
    return tuple(reasons)
