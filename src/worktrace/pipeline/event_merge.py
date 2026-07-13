from __future__ import annotations

from ..models import (
    MergedEventDraft,
    SourceBackedEventDraft,
    WorkEvent,
)
from ..utils.link_refs import sort_referenced_link_ids
from ..utils.hashing import evidence_fingerprint, stable_event_id
from ..utils.text import choose_preferred_text, merge_content_texts


def merge_duplicate_drafts(
    drafts: list[MergedEventDraft],
) -> tuple[list[MergedEventDraft], list[str]]:
    from collections import defaultdict

    grouped: dict[tuple[str, ...], list[MergedEventDraft]] = defaultdict(list)

    for draft in drafts:
        topic_key = " ".join(draft.topic.split())
        grouped[(tuple(draft.source_message_ids), topic_key)].append(draft)

    merged: list[MergedEventDraft] = []
    for (_group_key, items) in grouped.items():
        merged.append(
            MergedEventDraft(
                date=items[0].date,
                topic=choose_preferred_text([item.topic for item in items]),
                content=merge_content_texts([item.content for item in items]),
                object_hint=choose_preferred_text([item.object_hint for item in items]),
                retention_reason=choose_preferred_text(
                    [item.retention_reason for item in items]
                ),
                retention_detail=choose_preferred_text(
                    [item.retention_detail for item in items]
                ),
                referenced_link_ids=sort_referenced_link_ids(
                    [
                        link_id
                        for item in items
                        for link_id in item.referenced_link_ids
                    ],
                    message_order=list(items[0].source_message_ids),
                ),
                source_message_ids=list(items[0].source_message_ids),
                source_conversation_ids=sorted(
                    {cid for item in items for cid in item.source_conversation_ids}
                ),
                referenced_attachment_ids=list(
                    dict.fromkeys(
                        attachment_id
                        for item in items
                        for attachment_id in item.referenced_attachment_ids
                    )
                ),
                workstream_name=choose_preferred_text(
                    [item.workstream_name for item in items]
                ),
                action_labels=list(
                    dict.fromkeys(
                        label
                        for item in items
                        for label in item.action_labels
                        if label
                    )
                ),
                self_relations=list(
                    dict.fromkeys(
                        relation
                        for item in items
                        for relation in item.self_relations
                        if relation
                    )
                ),
            )
        )

    return merged, []


def build_work_events(
    target_date: str,
    drafts: list[MergedEventDraft],
) -> tuple[list[WorkEvent], list[str]]:
    merged_drafts, warnings = merge_duplicate_drafts(drafts)
    events: list[WorkEvent] = []
    seen_event_ids: set[str] = set()

    for draft in merged_drafts:
        event_id = stable_event_id(target_date, draft.source_message_ids, draft.content)
        if event_id in seen_event_ids:
            raise ValueError(f"Unresolvable event_id collision: {event_id}")
        seen_event_ids.add(event_id)
        events.append(
            WorkEvent(
                date=target_date,
                event_id=event_id,
                title=draft.topic,
                content=draft.content,
                source_message_ids=list(draft.source_message_ids),
                referenced_link_ids=list(draft.referenced_link_ids),
                object_hint=draft.object_hint,
                retention_reason=draft.retention_reason,
                retention_detail=draft.retention_detail,
                referenced_attachment_ids=list(draft.referenced_attachment_ids),
                workstream_name=draft.workstream_name,
                action_labels=list(draft.action_labels),
                self_relations=list(draft.self_relations),
                evidence_fingerprints=[
                    evidence_fingerprint(message_id)
                    for message_id in draft.source_message_ids
                ],
            )
        )

    return sorted(events, key=lambda item: item.event_id), warnings
