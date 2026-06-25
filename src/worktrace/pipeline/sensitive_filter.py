from __future__ import annotations

from ..config import RuntimeConfig
from ..models import MergedEventDraft
from ..utils.text import clean_text


def filter_sensitive_merged_drafts(
    drafts: list[MergedEventDraft],
    config: RuntimeConfig,
) -> tuple[list[MergedEventDraft], list[str]]:
    kept: list[MergedEventDraft] = []
    warnings: list[str] = []
    keywords = config.confidential_event_keywords + config.non_work_sensitive_keywords

    for draft in drafts:
        haystack = clean_text("\n".join([draft.topic, draft.content, draft.result]))
        if any(keyword in haystack for keyword in keywords):
            warnings.append(
                f"Filtered sensitive event draft: {draft.topic or '(empty topic)'}"
            )
            continue
        kept.append(draft)

    return kept, warnings
