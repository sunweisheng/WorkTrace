from __future__ import annotations

import re
from typing import Protocol

from ..config import RetentionPolicyConfig
from ..models import MergedEventDraft, SourceBackedEventDraft, WorkEvent
from ..utils.text import choose_preferred_text, clean_text


RETENTION_REASONS = {
    "deliverable_updated",
    "decision_made",
    "issue_or_risk_found",
    "follow_up_assigned",
    "external_business_progress",
    "substantive_approval",
}

_PUNCTUATION_RE = re.compile(r"[\s，。！？、,.!?:：；;（）()【】\\[\\]《》<>\"'“”‘’`~\-_/]+")


class RetentionCandidate(Protocol):
    topic: str
    content: str
    object_hint: str
    retention_reason: str
    retention_detail: str


class RetentionEvent(Protocol):
    title: str
    content: str
    object_hint: str
    retention_reason: str
    retention_detail: str


def filter_retained_candidate_drafts(
    drafts: list[SourceBackedEventDraft],
    policy: RetentionPolicyConfig,
) -> tuple[list[SourceBackedEventDraft], list[str]]:
    kept: list[SourceBackedEventDraft] = []
    warnings: list[str] = []
    for draft in drafts:
        reason = retention_rejection_reason(draft, policy)
        if reason:
            warnings.append(
                f"Filtered low-retention event draft: {draft.topic or '(empty topic)'} ({reason})"
            )
            continue
        kept.append(draft)
    return kept, warnings


def filter_retained_merged_drafts(
    drafts: list[MergedEventDraft],
    policy: RetentionPolicyConfig,
) -> tuple[list[MergedEventDraft], list[str]]:
    kept: list[MergedEventDraft] = []
    warnings: list[str] = []
    for draft in drafts:
        reason = retention_rejection_reason(draft, policy)
        if reason:
            warnings.append(
                f"Filtered low-retention event draft: {draft.topic or '(empty topic)'} ({reason})"
            )
            continue
        kept.append(draft)
    return kept, warnings


def filter_retained_work_events(
    events: list[WorkEvent],
    policy: RetentionPolicyConfig,
) -> tuple[list[WorkEvent], list[str]]:
    kept: list[WorkEvent] = []
    warnings: list[str] = []
    for event in events:
        reason = retention_rejection_reason_for_event(event, policy)
        if reason:
            warnings.append(
                f"Filtered low-retention event: {event.title or '(empty title)'} ({reason})"
            )
            continue
        kept.append(event)
    return kept, warnings


def retention_rejection_reason(
    candidate: RetentionCandidate,
    policy: RetentionPolicyConfig,
) -> str:
    reason = clean_text(candidate.retention_reason)
    detail = clean_text(candidate.retention_detail)
    object_hint = clean_text(candidate.object_hint)
    title = clean_text(candidate.topic)
    content = clean_text(candidate.content)

    if reason not in RETENTION_REASONS:
        return "missing_or_invalid_retention_reason"
    if _is_personal_privacy_or_leave_event(
        title, content, detail, object_hint, policy
    ):
        return "personal_privacy_or_leave_event"
    if _is_personal_social_or_reputation_event(title, content, detail, policy):
        return "personal_social_or_reputation_event"
    if _is_administrative_approval_event(
        title, content, detail, object_hint, policy
    ):
        return "administrative_approval_event"
    if _is_generic_review_completion(title, content, detail, object_hint, policy):
        return "generic_review_completion"
    if _is_generic_object_hint(object_hint, policy):
        return "missing_or_generic_object_hint"
    if not _has_specific_retention_detail(
        detail,
        title=title,
        content=content,
        object_hint=object_hint,
        policy=policy,
    ):
        return "missing_or_generic_retention_detail"
    return ""


def retention_rejection_reason_for_event(
    event: RetentionEvent,
    policy: RetentionPolicyConfig,
) -> str:
    title = clean_text(event.title)
    content = clean_text(event.content)
    object_hint = clean_text(event.object_hint)
    reason = clean_text(event.retention_reason)
    detail = clean_text(event.retention_detail)

    if reason not in RETENTION_REASONS:
        return "missing_or_invalid_retention_reason"
    if _is_personal_privacy_or_leave_event(
        title, content, detail, object_hint, policy
    ):
        return "personal_privacy_or_leave_event"
    if _is_personal_social_or_reputation_event(title, content, detail, policy):
        return "personal_social_or_reputation_event"
    if _is_administrative_approval_event(
        title, content, detail, object_hint, policy
    ):
        return "administrative_approval_event"
    if _is_generic_review_completion(title, content, detail, object_hint, policy):
        return "generic_review_completion"
    if _is_generic_object_hint(object_hint, policy):
        return "missing_or_generic_object_hint"
    if not _has_specific_retention_detail(
        detail,
        title=title,
        content=content,
        object_hint=object_hint,
        policy=policy,
    ):
        return "missing_or_generic_retention_detail"
    return ""


def derive_retention_metadata_from_sources(
    items: list[SourceBackedEventDraft],
) -> tuple[str, str, str]:
    return (
        choose_preferred_text([item.object_hint for item in items]),
        choose_preferred_text([item.retention_reason for item in items]),
        choose_preferred_text([item.retention_detail for item in items]),
    )


def _is_generic_object_hint(value: str, policy: RetentionPolicyConfig) -> bool:
    compact = _normalized_compact(value)
    if not compact:
        return True
    return compact in {
        _normalized_compact(item) for item in policy.generic_object_hints
    }


def _is_personal_social_or_reputation_event(
    title: str,
    content: str,
    detail: str,
    policy: RetentionPolicyConfig,
) -> bool:
    combined = _normalized_compact(" ".join([title, content, detail]))
    if not any(
        _normalized_compact(keyword) in combined
        for keyword in policy.personal_social_keywords
    ):
        return False
    return not _has_substantive_work_signal(combined, policy)


def _is_personal_privacy_or_leave_event(
    title: str,
    content: str,
    detail: str,
    object_hint: str,
    policy: RetentionPolicyConfig,
) -> bool:
    compact_object = _normalized_compact(object_hint)
    if compact_object and any(
        _normalized_compact(keyword) in compact_object
        for keyword in policy.personal_privacy_object_hints
    ):
        return True

    combined = _normalized_compact(" ".join([title, content, detail, object_hint]))
    has_leave_signal = any(
        _normalized_compact(keyword) in combined
        for keyword in policy.personal_leave_or_travel_keywords
    )
    has_private_reason = any(
        _normalized_compact(keyword) in combined
        for keyword in policy.personal_private_reason_keywords
    )
    return has_leave_signal and has_private_reason


def _is_generic_review_completion(
    title: str,
    content: str,
    detail: str,
    object_hint: str,
    policy: RetentionPolicyConfig,
) -> bool:
    combined = _normalized_compact(" ".join([title, content, detail, object_hint]))
    if not any(
        _normalized_compact(keyword) in combined
        for keyword in policy.generic_review_keywords
    ):
        return False
    return not _has_substantive_work_signal(combined, policy)


def _is_administrative_approval_event(
    title: str,
    content: str,
    detail: str,
    object_hint: str,
    policy: RetentionPolicyConfig,
) -> bool:
    combined = _normalized_compact(" ".join([title, content, detail, object_hint]))
    has_review_signal = any(
        _normalized_compact(keyword) in combined
        for keyword in (
            policy.generic_review_keywords + policy.approval_action_keywords
        )
    )
    if not has_review_signal:
        return False
    has_admin_signal = any(
        _normalized_compact(keyword) in combined
        for keyword in policy.administrative_approval_keywords
    )
    if not has_admin_signal:
        return False
    return not _has_substantive_work_signal(combined, policy)


def _has_substantive_work_signal(
    compact_text: str,
    policy: RetentionPolicyConfig,
) -> bool:
    return any(
        _normalized_compact(keyword) in compact_text
        for keyword in policy.substantive_work_keywords
    )


def _has_specific_retention_detail(
    detail: str,
    *,
    title: str,
    content: str,
    object_hint: str,
    policy: RetentionPolicyConfig,
) -> bool:
    compact_detail = _normalized_compact(detail)
    if len(compact_detail) < 6:
        return False
    if compact_detail in {
        _normalized_compact(title),
        _normalized_compact(content),
        _normalized_compact(object_hint),
    }:
        return False
    if _is_repeated_low_information(title, detail, policy):
        return False
    return True


def _is_repeated_low_information(
    title: str,
    content: str,
    policy: RetentionPolicyConfig,
) -> bool:
    compact_title = _normalized_compact(title)
    compact_content = _normalized_compact(content)
    if not compact_title or not compact_content:
        return False
    return len(compact_content) <= 16 and (
        compact_content == compact_title
        or any(
            compact_content == compact_title + _normalized_compact(suffix)
            for suffix in policy.repeated_low_information_suffixes
        )
    )


def _normalized_compact(value: str) -> str:
    return _PUNCTUATION_RE.sub("", clean_text(value)).lower()
