from __future__ import annotations

import re
from typing import Protocol

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

GENERIC_OBJECT_HINTS = {
    "审核",
    "审批",
    "工作审核",
    "审核任务",
    "业务审批",
    "审核结果",
    "审批结果",
    "会议",
    "沟通",
    "工作",
    "事项",
    "任务",
    "安排",
    "确认",
    "同步",
    "信息",
}
_PUNCTUATION_RE = re.compile(r"[\s，。！？、,.!?:：；;（）()【】\\[\\]《》<>\"'“”‘’`~\-_/]+")
_PERSONAL_SOCIAL_KEYWORDS = (
    "约饭",
    "吃饭",
    "聚餐",
    "饭局",
    "火锅",
    "牛蛙",
    "告别",
    "离职前",
    "口碑",
    "评价不错",
    "评价良好",
    "人际",
    "寒暄",
    "回家",
    "收拾东西",
)
_PERSONAL_LEAVE_OR_TRAVEL_KEYWORDS = (
    "请假",
    "晚到",
    "晚来",
    "迟到",
    "外出",
    "行程报备",
    "不在公司",
    "个人事由",
    "私事",
    "家事",
)
_PERSONAL_PRIVATE_REASON_KEYWORDS = (
    "孩子",
    "学校",
    "证明",
    "医院",
    "家里",
    "家庭",
    "个人隐私",
)
_PERSONAL_PRIVACY_OBJECT_HINTS = (
    "个人请假",
    "个人外出事由",
    "个人请假/外出事由",
)
_GENERIC_REVIEW_KEYWORDS = (
    "完成审核",
    "完成审批",
    "完成了审核",
    "完成了审批",
    "工作审核",
    "审核任务",
    "业务审批",
    "审核结果",
    "审批结果",
    "同步审核",
    "同步审批",
    "闭环",
)
_APPROVAL_ACTION_KEYWORDS = (
    "审批",
    "审核",
)
_ADMIN_APPROVAL_KEYWORDS = (
    "加班",
    "考勤",
    "补卡",
    "请假",
    "调休",
    "外出报备",
    "出勤",
)
_SUBSTANTIVE_WORK_KEYWORDS = (
    "合同",
    "付款",
    "发票",
    "客户",
    "项目",
    "文档",
    "方案",
    "需求",
    "发布",
    "上线",
    "数据",
    "金额",
    "条款",
    "风险",
    "问题",
    "缺少",
    "缺失",
    "驳回",
    "拒绝",
    "通过",
    "批准",
    "补充",
    "修改",
    "调整",
    "结论",
    "排期",
)


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
) -> tuple[list[SourceBackedEventDraft], list[str]]:
    kept: list[SourceBackedEventDraft] = []
    warnings: list[str] = []
    for draft in drafts:
        reason = retention_rejection_reason(draft)
        if reason:
            warnings.append(
                f"Filtered low-retention event draft: {draft.topic or '(empty topic)'} ({reason})"
            )
            continue
        kept.append(draft)
    return kept, warnings


def filter_retained_merged_drafts(
    drafts: list[MergedEventDraft],
) -> tuple[list[MergedEventDraft], list[str]]:
    kept: list[MergedEventDraft] = []
    warnings: list[str] = []
    for draft in drafts:
        reason = retention_rejection_reason(draft)
        if reason:
            warnings.append(
                f"Filtered low-retention event draft: {draft.topic or '(empty topic)'} ({reason})"
            )
            continue
        kept.append(draft)
    return kept, warnings


def filter_retained_work_events(
    events: list[WorkEvent],
) -> tuple[list[WorkEvent], list[str]]:
    kept: list[WorkEvent] = []
    warnings: list[str] = []
    for event in events:
        reason = retention_rejection_reason_for_event(
            event,
        )
        if reason:
            warnings.append(
                f"Filtered low-retention event: {event.title or '(empty title)'} ({reason})"
            )
            continue
        kept.append(event)
    return kept, warnings


def retention_rejection_reason(candidate: RetentionCandidate) -> str:
    reason = clean_text(candidate.retention_reason)
    detail = clean_text(candidate.retention_detail)
    object_hint = clean_text(candidate.object_hint)
    title = clean_text(candidate.topic)
    content = clean_text(candidate.content)

    if reason not in RETENTION_REASONS:
        return "missing_or_invalid_retention_reason"
    if _is_personal_privacy_or_leave_event(title, content, detail, object_hint):
        return "personal_privacy_or_leave_event"
    if _is_personal_social_or_reputation_event(title, content, detail):
        return "personal_social_or_reputation_event"
    if _is_administrative_approval_event(title, content, detail, object_hint):
        return "administrative_approval_event"
    if _is_generic_review_completion(title, content, detail, object_hint):
        return "generic_review_completion"
    if _is_generic_object_hint(object_hint):
        return "missing_or_generic_object_hint"
    if not _has_specific_retention_detail(detail, title=title, content=content, object_hint=object_hint):
        return "missing_or_generic_retention_detail"
    return ""


def retention_rejection_reason_for_event(
    event: RetentionEvent,
) -> str:
    title = clean_text(event.title)
    content = clean_text(event.content)
    object_hint = clean_text(event.object_hint)
    reason = clean_text(event.retention_reason)
    detail = clean_text(event.retention_detail)

    if reason not in RETENTION_REASONS:
        return "missing_or_invalid_retention_reason"
    if _is_personal_privacy_or_leave_event(title, content, detail, object_hint):
        return "personal_privacy_or_leave_event"
    if _is_personal_social_or_reputation_event(title, content, detail):
        return "personal_social_or_reputation_event"
    if _is_administrative_approval_event(title, content, detail, object_hint):
        return "administrative_approval_event"
    if _is_generic_review_completion(title, content, detail, object_hint):
        return "generic_review_completion"
    if _is_generic_object_hint(object_hint):
        return "missing_or_generic_object_hint"
    if not _has_specific_retention_detail(detail, title=title, content=content, object_hint=object_hint):
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


def _is_generic_object_hint(value: str) -> bool:
    compact = _normalized_compact(value)
    if not compact:
        return True
    return compact in {_normalized_compact(item) for item in GENERIC_OBJECT_HINTS}


def _is_personal_social_or_reputation_event(
    title: str,
    content: str,
    detail: str,
) -> bool:
    combined = _normalized_compact(" ".join([title, content, detail]))
    if not any(_normalized_compact(keyword) in combined for keyword in _PERSONAL_SOCIAL_KEYWORDS):
        return False
    return not _has_substantive_work_signal(combined)


def _is_personal_privacy_or_leave_event(
    title: str,
    content: str,
    detail: str,
    object_hint: str,
) -> bool:
    compact_object = _normalized_compact(object_hint)
    if compact_object and any(
        _normalized_compact(keyword) in compact_object
        for keyword in _PERSONAL_PRIVACY_OBJECT_HINTS
    ):
        return True

    combined = _normalized_compact(" ".join([title, content, detail, object_hint]))
    has_leave_signal = any(
        _normalized_compact(keyword) in combined
        for keyword in _PERSONAL_LEAVE_OR_TRAVEL_KEYWORDS
    )
    has_private_reason = any(
        _normalized_compact(keyword) in combined
        for keyword in _PERSONAL_PRIVATE_REASON_KEYWORDS
    )
    return has_leave_signal and has_private_reason


def _is_generic_review_completion(
    title: str,
    content: str,
    detail: str,
    object_hint: str,
) -> bool:
    combined = _normalized_compact(" ".join([title, content, detail, object_hint]))
    if not any(_normalized_compact(keyword) in combined for keyword in _GENERIC_REVIEW_KEYWORDS):
        return False
    return not _has_substantive_work_signal(combined)


def _is_administrative_approval_event(
    title: str,
    content: str,
    detail: str,
    object_hint: str,
) -> bool:
    combined = _normalized_compact(" ".join([title, content, detail, object_hint]))
    has_review_signal = any(
        _normalized_compact(keyword) in combined
        for keyword in (_GENERIC_REVIEW_KEYWORDS + _APPROVAL_ACTION_KEYWORDS)
    )
    if not has_review_signal:
        return False
    has_admin_signal = any(
        _normalized_compact(keyword) in combined for keyword in _ADMIN_APPROVAL_KEYWORDS
    )
    if not has_admin_signal:
        return False
    return not _has_substantive_work_signal(combined)


def _has_substantive_work_signal(compact_text: str) -> bool:
    return any(
        _normalized_compact(keyword) in compact_text
        for keyword in _SUBSTANTIVE_WORK_KEYWORDS
    )


def _has_specific_retention_detail(
    detail: str,
    *,
    title: str,
    content: str,
    object_hint: str,
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
    if _is_repeated_low_information(title, detail):
        return False
    return True


def _is_repeated_low_information(title: str, content: str) -> bool:
    compact_title = _normalized_compact(title)
    compact_content = _normalized_compact(content)
    if not compact_title or not compact_content:
        return False
    suffixes = ("工作", "事项", "任务", "相关工作", "相关事项")
    return len(compact_content) <= 16 and (
        compact_content == compact_title
        or any(compact_content == compact_title + _normalized_compact(suffix) for suffix in suffixes)
    )


def _normalized_compact(value: str) -> str:
    return _PUNCTUATION_RE.sub("", clean_text(value)).lower()
