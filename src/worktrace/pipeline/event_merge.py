from __future__ import annotations

from ..models import (
    MergedEventDraft,
    SourceBackedEventDraft,
    WorkEvent,
)
from ..utils.hashing import stable_event_id
from ..utils.text import choose_preferred_text, merge_content_texts


def merge_duplicate_drafts(
    drafts: list[MergedEventDraft],
) -> tuple[list[MergedEventDraft], list[str]]:
    from collections import defaultdict

    grouped: dict[tuple[str, ...], list[MergedEventDraft]] = defaultdict(list)

    for draft in drafts:
        grouped[tuple(draft.source_message_ids)].append(draft)

    merged: list[MergedEventDraft] = []
    for message_ids, items in grouped.items():
        merged.append(
            MergedEventDraft(
                date=items[0].date,
                topic=choose_preferred_text([item.topic for item in items]),
                content=merge_content_texts([item.content for item in items]),
                source_message_ids=list(message_ids),
                source_conversation_ids=sorted(
                    {cid for item in items for cid in item.source_conversation_ids}
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
        event_id = stable_event_id(target_date, draft.source_message_ids)
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
            )
        )

    return sorted(events, key=lambda item: item.event_id), warnings
