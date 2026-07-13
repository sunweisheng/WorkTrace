from __future__ import annotations

from ..models import CrossConversationGroup, MergedEventDraft, SourceBackedEventDraft
from .retention_filter import derive_retention_metadata_from_sources
from ..utils.link_refs import sort_referenced_link_ids
from ..utils.text import choose_preferred_text, clean_text, merge_content_texts


def consolidate_workstream_groups(
    groups: list[CrossConversationGroup],
    candidates: list[SourceBackedEventDraft],
) -> tuple[list[CrossConversationGroup], list[str]]:
    candidate_by_id = {candidate.draft_id: candidate for candidate in candidates}
    groups_by_key: dict[str, list[str]] = {}
    standalone_ids: list[str] = []

    for candidate in candidates:
        key = "".join(clean_text(candidate.workstream_key).casefold().split())
        if key:
            groups_by_key.setdefault(key, []).append(candidate.draft_id)
        else:
            standalone_ids.append(candidate.draft_id)

    consolidated = [
        CrossConversationGroup(
            group_id=f"workstream-{key}",
            draft_ids=draft_ids,
            primary_draft_id=_select_primary_draft_id(
                draft_ids,
                candidate_by_id,
            ),
            workstream_name=next(
                (
                    candidate_by_id[draft_id].workstream_key.strip()
                    for draft_id in draft_ids
                    if candidate_by_id[draft_id].workstream_key.strip()
                ),
                "",
            ),
        )
        for key, draft_ids in groups_by_key.items()
    ]
    consolidated.extend(
        CrossConversationGroup(
            group_id=f"standalone-{draft_id}",
            draft_ids=[draft_id],
            primary_draft_id=draft_id,
            workstream_name=candidate_by_id[draft_id].workstream_key.strip(),
        )
        for draft_id in standalone_ids
    )
    return consolidated, []


def _select_primary_draft_id(
    draft_ids: list[str],
    candidate_by_id: dict[str, SourceBackedEventDraft],
) -> str:
    for draft_id in draft_ids:
        candidate = candidate_by_id[draft_id]
        if candidate.workstream_key and candidate.workstream_key.casefold() in (
            f"{candidate.topic} {candidate.object_hint}".casefold()
        ):
            return draft_id
    return draft_ids[0] if draft_ids else ""


def materialize_grouped_merged_drafts(
    candidates: list[SourceBackedEventDraft],
    groups: list[CrossConversationGroup],
    *,
    target_date: str,
    message_order: list[str],
    self_relation_order: tuple[str, ...] = (),
) -> list[MergedEventDraft]:
    draft_map = {candidate.draft_id: candidate for candidate in candidates}
    candidate_order = {candidate.draft_id: index for index, candidate in enumerate(candidates)}
    message_positions = {message_id: index for index, message_id in enumerate(message_order)}
    merged_drafts: list[MergedEventDraft] = []

    for group in groups:
        items = [
            draft_map[draft_id]
            for draft_id in group.draft_ids
            if draft_id in draft_map
        ]
        if not items:
            continue
        items.sort(
            key=lambda item: _candidate_sort_key(
                item,
                message_positions=message_positions,
                candidate_order=candidate_order,
            )
        )
        primary = draft_map.get(group.primary_draft_id)
        if primary not in items:
            primary = items[0]
        object_hint, retention_reason, retention_detail = (
            derive_retention_metadata_from_sources(items)
        )
        source_message_ids = _ordered_source_message_ids(items, message_order)
        merged_drafts.append(
            MergedEventDraft(
                date=target_date,
                topic=primary.topic or choose_preferred_text([item.topic for item in items]),
                content=merge_content_texts([item.content for item in items]),
                object_hint=primary.object_hint or object_hint,
                retention_reason=retention_reason,
                retention_detail=retention_detail,
                workstream_name=group.workstream_name.strip(),
                action_labels=list(
                    dict.fromkeys(
                        item.action_label.strip()
                        for item in items
                        if item.action_label.strip()
                    )
                ),
                self_relations=_merge_self_relations(
                    items,
                    relation_order=self_relation_order,
                ),
                referenced_link_ids=sort_referenced_link_ids(
                    [
                        link_id
                        for item in items
                        for link_id in item.referenced_link_ids
                    ],
                    message_order=source_message_ids,
                ),
                referenced_attachment_ids=list(
                    dict.fromkeys(
                        attachment_id
                        for item in items
                        for attachment_id in item.referenced_attachment_ids
                    )
                ),
                source_message_ids=source_message_ids,
                source_conversation_ids=sorted(
                    {
                        conversation_id
                        for item in items
                        for conversation_id in [item.source_conversation_id]
                    }
                ),
            )
        )

    return merged_drafts


def _merge_self_relations(
    items: list[SourceBackedEventDraft],
    *,
    relation_order: tuple[str, ...],
) -> list[str]:
    encountered = list(
        dict.fromkeys(
            relation.relation
            for item in items
            for relation in item.self_relations
            if relation.relation
        )
    )
    if not relation_order:
        return encountered
    configured = [relation for relation in relation_order if relation in encountered]
    return [*configured, *(relation for relation in encountered if relation not in configured)]


def _candidate_sort_key(
    candidate: SourceBackedEventDraft,
    *,
    message_positions: dict[str, int],
    candidate_order: dict[str, int],
) -> tuple[int, int]:
    positions = [
        message_positions[message_id]
        for message_id in candidate.source_message_ids
        if message_id in message_positions
    ]
    return (
        min(positions) if positions else len(message_positions),
        candidate_order.get(candidate.draft_id, len(candidate_order)),
    )


def _ordered_source_message_ids(
    items: list[SourceBackedEventDraft],
    message_order: list[str],
) -> list[str]:
    source_ids = {
        message_id
        for item in items
        for message_id in item.source_message_ids
    }
    ordered = [message_id for message_id in message_order if message_id in source_ids]
    remaining = sorted(source_ids - set(ordered))
    return [*ordered, *remaining]
