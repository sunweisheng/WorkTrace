from __future__ import annotations

from ..models import MergedEventDraft, SourceBackedEventDraft
from .retention_filter import derive_retention_metadata_from_sources
from ..utils.text import choose_preferred_text, merge_content_texts


def materialize_grouped_merged_drafts(
    candidates: list[SourceBackedEventDraft],
    groups: list[list[str]],
    *,
    target_date: str,
) -> list[MergedEventDraft]:
    draft_map = {candidate.draft_id: candidate for candidate in candidates}
    merged_drafts: list[MergedEventDraft] = []

    for group in groups:
        items = [draft_map[draft_id] for draft_id in group if draft_id in draft_map]
        if not items:
            continue
        object_hint, retention_reason, retention_detail = (
            derive_retention_metadata_from_sources(items)
        )
        merged_drafts.append(
            MergedEventDraft(
                date=target_date,
                topic=choose_preferred_text([item.topic for item in items]),
                content=merge_content_texts([item.content for item in items]),
                object_hint=object_hint,
                retention_reason=retention_reason,
                retention_detail=retention_detail,
                source_message_ids=sorted(
                    {
                        message_id
                        for item in items
                        for message_id in item.source_message_ids
                    }
                ),
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
