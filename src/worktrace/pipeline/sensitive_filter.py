from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TypeVar

from ..config import RuntimeConfig
from ..models import MergedEventDraft, SourceBackedEventDraft, WorkEvent
from ..utils.text import clean_text


T = TypeVar("T")


@dataclass(frozen=True)
class FilterDiagnostic:
    item_index: int
    kind: str


def filter_candidate_drafts(
    drafts: list[SourceBackedEventDraft],
    config: RuntimeConfig,
) -> tuple[list[SourceBackedEventDraft], list[str]]:
    return _filter_items(
        drafts,
        config,
        text_fields=lambda draft: (
            draft.topic,
            draft.content,
            draft.action_label,
            draft.object_hint,
            draft.retention_detail,
            draft.workstream_key,
        ),
    )


def filter_merged_drafts(
    drafts: list[MergedEventDraft],
    config: RuntimeConfig,
) -> tuple[list[MergedEventDraft], list[str]]:
    return _filter_items(
        drafts,
        config,
        text_fields=lambda draft: (
            draft.topic,
            draft.content,
            *draft.action_labels,
            draft.object_hint,
            draft.retention_detail,
            draft.workstream_name,
        ),
    )


def filter_work_events(
    events: list[WorkEvent],
    config: RuntimeConfig,
) -> tuple[list[WorkEvent], list[str]]:
    kept, diagnostics = filter_work_events_with_diagnostics(events, config)
    return kept, [_warning_for_diagnostic(item) for item in diagnostics]


def filter_work_events_with_diagnostics(
    events: list[WorkEvent],
    config: RuntimeConfig,
) -> tuple[list[WorkEvent], list[FilterDiagnostic]]:
    return _filter_items_with_diagnostics(
        events,
        config,
        text_fields=lambda event: (
            event.title,
            event.content,
            *event.action_labels,
            event.object_hint,
            event.retention_detail,
            event.workstream_name,
            *(link.title for link in event.file_links),
            *(link.url for link in event.file_links),
        ),
    )


def _warning_for_diagnostic(diagnostic: FilterDiagnostic) -> str:
    if diagnostic.kind == "sensitive":
        return "Filtered sensitive event."
    return "Filtered excluded event."


def _filter_items_with_diagnostics(
    items: list[T],
    config: RuntimeConfig,
    *,
    text_fields: Callable[[T], tuple[str, ...]],
) -> tuple[list[T], list[FilterDiagnostic]]:
    sensitive_keywords = _normalized_keywords(config.sensitive_event_keywords)
    excluded_keywords = _normalized_keywords(config.excluded_event_keywords)
    kept: list[T] = []
    diagnostics: list[FilterDiagnostic] = []

    for item_index, item in enumerate(items):
        fields = tuple(
            normalized
            for value in text_fields(item)
            if (normalized := clean_text(value))
        )
        if _contains_keyword(fields, sensitive_keywords):
            diagnostics.append(FilterDiagnostic(item_index, "sensitive"))
            continue
        if _contains_keyword(fields, excluded_keywords):
            diagnostics.append(FilterDiagnostic(item_index, "excluded"))
            continue
        kept.append(item)

    return kept, diagnostics


def _filter_items(
    items: list[T],
    config: RuntimeConfig,
    *,
    text_fields: Callable[[T], tuple[str, ...]],
) -> tuple[list[T], list[str]]:
    sensitive_keywords = _normalized_keywords(config.sensitive_event_keywords)
    excluded_keywords = _normalized_keywords(config.excluded_event_keywords)
    kept: list[T] = []
    warnings: list[str] = []

    for item in items:
        fields = tuple(
            normalized
            for value in text_fields(item)
            if (normalized := clean_text(value))
        )
        if _contains_keyword(fields, sensitive_keywords):
            warnings.append("Filtered sensitive event.")
            continue
        if _contains_keyword(fields, excluded_keywords):
            warnings.append("Filtered excluded event.")
            continue
        kept.append(item)

    return kept, warnings


def _normalized_keywords(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(cleaned for value in values if (cleaned := clean_text(value)))


def _contains_keyword(fields: tuple[str, ...], keywords: tuple[str, ...]) -> bool:
    return any(keyword in field for keyword in keywords for field in fields)
