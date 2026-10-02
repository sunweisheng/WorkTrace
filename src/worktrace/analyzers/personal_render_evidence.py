from __future__ import annotations

from datetime import datetime
from typing import Sequence

from ..models import ConversationSlice, NormalizedMessage, SourceBackedEventDraft


def personal_render_evidence(
    candidates: Sequence[SourceBackedEventDraft],
    messages: Sequence[NormalizedMessage],
    conversation_slices: Sequence[ConversationSlice] = (),
) -> tuple[list[str], list[dict[str, str]]]:
    """Add earlier identical full bodies as context, without assigning credit."""
    message_ids = list(dict.fromkeys(
        message_id
        for item in candidates
        for message_id in [
            *item.source_message_ids, *item.self_evidence_message_ids,
        ]
    ))
    selected = set(message_ids)
    slice_ids = {item.source_slice_id for item in candidates}
    related_ids = selected | {
        message.message_id
        for item in conversation_slices
        if item.slice_id in slice_ids
        for message in item.messages
    }
    earlier: dict[tuple[str, str], list[str]] = {}
    relations: list[dict[str, str]] = []
    for message in sorted(
        messages,
        key=lambda item: datetime.fromisoformat(item.send_time).timestamp(),
    ):
        if message.message_id not in related_ids:
            continue
        body = message.text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not body:
            continue
        key = (message.conversation_id, body)
        if message.message_id in selected:
            for prior_id in earlier.get(key, []):
                relations.append({
                    "earlier_message_id": prior_id,
                    "later_message_id": message.message_id,
                })
                message_ids.append(prior_id)
        earlier.setdefault(key, []).append(message.message_id)
    return list(dict.fromkeys(message_ids)), relations
