from __future__ import annotations

from dataclasses import dataclass
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
