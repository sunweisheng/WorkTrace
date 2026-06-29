from __future__ import annotations

from ..config import RuntimeConfig
from ..models import MergedEventDraft, SourceBackedEventDraft
from ..utils.text import clean_text


def filter_excluded_candidate_drafts(
    drafts: list[SourceBackedEventDraft],
    config: RuntimeConfig,
) -> tuple[list[SourceBackedEventDraft], list[str]]:
    kept: list[SourceBackedEventDraft] = []
    warnings: list[str] = []
    excluded_topics = {clean_text(topic) for topic in config.excluded_event_topics}
    excluded_signatures = {
        clean_text(signature) for signature in config.excluded_event_content_signatures
    }

    for draft in drafts:
        normalized_topic = clean_text(draft.topic)
        normalized_content = clean_text(draft.content)
        if normalized_topic in excluded_topics or any(
            signature in normalized_content for signature in excluded_signatures
        ):
            warnings.append(
                f"Filtered excluded event draft: {draft.topic or '(empty topic)'}"
            )
            continue
        kept.append(draft)

    return kept, warnings


def filter_sensitive_merged_drafts(
    drafts: list[MergedEventDraft],
    config: RuntimeConfig,
) -> tuple[list[MergedEventDraft], list[str]]:
    kept: list[MergedEventDraft] = []
    warnings: list[str] = []
    keywords = config.confidential_event_keywords + config.non_work_sensitive_keywords
    excluded_topics = {clean_text(topic) for topic in config.excluded_event_topics}

    for draft in drafts:
        haystack = clean_text("\n".join([draft.topic, draft.content]))
        if any(keyword in haystack for keyword in keywords):
            warnings.append(
                f"Filtered sensitive event draft: {draft.topic or '(empty topic)'}"
            )
            continue
        if clean_text(draft.topic) in excluded_topics:
            warnings.append(
                f"Filtered excluded event draft: {draft.topic or '(empty topic)'}"
            )
            continue
        kept.append(draft)

    return kept, warnings
