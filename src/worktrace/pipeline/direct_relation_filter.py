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
    self_assignment_cues: tuple[str, ...],
    self_assignment_actions: tuple[str, ...],
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
            self_assignment_cues=self_assignment_cues,
            self_assignment_actions=self_assignment_actions,
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
    self_assignment_cues: tuple[str, ...],
    self_assignment_actions: tuple[str, ...],
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
            self_assignment_cues=self_assignment_cues,
            self_assignment_actions=self_assignment_actions,
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
    self_assignment_cues: tuple[str, ...],
    self_assignment_actions: tuple[str, ...],
) -> bool:
    name = clean_text(self_display_name)
    assignment_cues = _clean_rule_terms(self_assignment_cues)
    assignment_actions = _clean_rule_terms(self_assignment_actions)
    if not name or not assignment_actions:
        return False

    normalized_text = _normalize_message_text(text)
    if not normalized_text:
        return False

    mentions = _mention_variants(name)
    for clause in _CLAUSE_SPLIT_RE.split(normalized_text):
        clause = clause.strip()
        if not clause or not any(mention in clause for mention in mentions):
            continue
        if _clause_assigns_action_to_mention(
            clause,
            mentions,
            assignment_cues=assignment_cues,
            assignment_actions=assignment_actions,
        ):
            return True
    return False


def _normalize_message_text(text: str) -> str:
    text = _AT_TAG_RE.sub(lambda match: f"@{match.group(1) or ''}", text)
    text = _HTML_TAG_RE.sub("", text)
    text = _WHITESPACE_RE.sub("", text)
    return clean_text(text)


def _mention_variants(name: str) -> tuple[str, ...]:
    return (f"@{name}", name)


def _clause_assigns_action_to_mention(
    clause: str,
    mentions: tuple[str, ...],
    *,
    assignment_cues: tuple[str, ...],
    assignment_actions: tuple[str, ...],
) -> bool:
    for mention in mentions:
        if mention not in clause:
            continue
        mention_index = clause.find(mention)
        before = clause[max(0, mention_index - 16) : mention_index]
        after = clause[mention_index + len(mention) : mention_index + len(mention) + 24]
        if _has_assignment_cue(before + after, assignment_cues) and _has_direct_action(
            after,
            assignment_actions,
        ):
            return True
        if after.startswith("你") and _has_direct_action(after[:12], assignment_actions):
            return True
        if _starts_with_direct_action(after, assignment_actions):
            return True
    return False


def _clean_rule_terms(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(cleaned for value in values if (cleaned := clean_text(value)))


def _has_assignment_cue(value: str, assignment_cues: tuple[str, ...]) -> bool:
    return any(cue in value for cue in assignment_cues)


def _has_direct_action(value: str, assignment_actions: tuple[str, ...]) -> bool:
    return any(action in value for action in assignment_actions)


def _starts_with_direct_action(
    value: str,
    assignment_actions: tuple[str, ...],
) -> bool:
    value = value.lstrip("：:，,。.!！?？ ")
    if value.startswith("你"):
        value = value[1:]
    return any(value.startswith(action) for action in assignment_actions)
