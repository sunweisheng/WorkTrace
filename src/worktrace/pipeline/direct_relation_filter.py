from __future__ import annotations

import re

from ..models import ConversationSlice, NormalizedMessage, SourceBackedEventDraft
from ..utils.text import clean_text


_AT_TAG_RE = re.compile(r"<at\b[^>]*>(.*?)</at>|<at\b[^>]*/>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_CLAUSE_SPLIT_RE = re.compile(r"[\n\r。！？!?；;，,、]+")
_WHITESPACE_RE = re.compile(r"\s+")


def filter_self_related_candidate_drafts(
    drafts: list[SourceBackedEventDraft],
    slices_by_id: dict[str, ConversationSlice],
    *,
    self_open_id: str,
    self_display_name: str,
    self_assignment_keywords: tuple[str, ...],
) -> tuple[list[SourceBackedEventDraft], list[str]]:
    kept: list[SourceBackedEventDraft] = []
    warnings: list[str] = []

    for draft in drafts:
        conversation_slice = slices_by_id.get(draft.source_slice_id)
        if conversation_slice is None:
            warnings.append(
                f"Filtered non-self-related event draft: {draft.topic or '(empty topic)'}"
            )
            continue
        if is_self_related_candidate_draft(
            draft,
            conversation_slice,
            self_open_id=self_open_id,
            self_display_name=self_display_name,
            self_assignment_keywords=self_assignment_keywords,
        ):
            kept.append(draft)
            continue
        warnings.append(
            f"Filtered non-self-related event draft: {draft.topic or '(empty topic)'}"
        )

    return kept, warnings


def is_self_related_candidate_draft(
    draft: SourceBackedEventDraft,
    conversation_slice: ConversationSlice,
    *,
    self_open_id: str,
    self_display_name: str,
    self_assignment_keywords: tuple[str, ...],
) -> bool:
    message_by_id = {message.message_id: message for message in conversation_slice.messages}
    source_messages = [
        message_by_id[message_id]
        for message_id in draft.source_message_ids
        if message_id in message_by_id
    ]
    if not source_messages:
        return False

    if any(message.sender_open_id == self_open_id for message in source_messages):
        return True

    anchor_ids = set(conversation_slice.anchor_message_ids)
    anchor_messages = [
        message_by_id[message_id]
        for message_id in conversation_slice.anchor_message_ids
        if message_id in message_by_id
    ]
    if any(
        _has_direct_anchor_relation(message, anchor_ids, anchor_messages)
        for message in source_messages
    ):
        return True

    return any(
        _explicitly_assigns_to_self(
            message.text,
            self_display_name,
            self_assignment_keywords=self_assignment_keywords,
        )
        for message in source_messages
    )


def _has_direct_anchor_relation(
    source_message: NormalizedMessage,
    anchor_ids: set[str],
    anchor_messages: list[NormalizedMessage],
) -> bool:
    if source_message.reply_to_message_id in anchor_ids:
        return True
    if source_message.quote_message_id in anchor_ids:
        return True

    source_id = source_message.message_id
    return any(
        anchor.reply_to_message_id == source_id or anchor.quote_message_id == source_id
        for anchor in anchor_messages
    )


def _explicitly_assigns_to_self(
    text: str,
    self_display_name: str,
    *,
    self_assignment_keywords: tuple[str, ...],
) -> bool:
    name = clean_text(self_display_name)
    assignment_keywords = _clean_rule_terms(self_assignment_keywords)
    if not name or not assignment_keywords:
        return False

    normalized_text = _normalize_message_text(text)
    if not normalized_text:
        return False

    mentions = _mention_variants(name)
    for clause in _CLAUSE_SPLIT_RE.split(normalized_text):
        clause = clause.strip()
        if not clause or not any(mention in clause for mention in mentions):
            continue
        if any(keyword in clause for keyword in assignment_keywords):
            return True
    return False


def _normalize_message_text(text: str) -> str:
    text = _AT_TAG_RE.sub(lambda match: f"@{match.group(1) or ''}", text)
    text = _HTML_TAG_RE.sub("", text)
    text = _WHITESPACE_RE.sub("", text)
    return clean_text(text)


def _mention_variants(name: str) -> tuple[str, ...]:
    return (f"@{name}", name)


def _clean_rule_terms(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(cleaned for value in values if (cleaned := clean_text(value)))
