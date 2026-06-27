from __future__ import annotations

from src.worktrace.config import RuntimeConfig
from src.worktrace.models import MergedEventDraft
from src.worktrace.pipeline.sensitive_filter import filter_sensitive_merged_drafts


def test_sensitive_filter_removes_salary_and_performance_drafts() -> None:
    drafts = [
        MergedEventDraft(
            date="2026-06-23",
            topic="工资调整沟通",
            content="讨论工资调整方案",
            source_message_ids=["m1"],
            source_conversation_ids=["c1"],
        ),
        MergedEventDraft(
            date="2026-06-23",
            topic="项目发布推进",
            content="确认上线节奏",
            source_message_ids=["m2"],
            source_conversation_ids=["c2"],
        ),
    ]

    kept, warnings = filter_sensitive_merged_drafts(drafts, RuntimeConfig())

    assert len(kept) == 1
    assert kept[0].topic == "项目发布推进"
    assert len(warnings) == 1


def test_sensitive_filter_removes_quarrel_and_abuse_drafts() -> None:
    drafts = [
        MergedEventDraft(
            date="2026-06-23",
            topic="团队吵架记录",
            content="双方在群里互骂并出现侮辱性表达",
            source_message_ids=["m1"],
            source_conversation_ids=["c1"],
        ),
        MergedEventDraft(
            date="2026-06-23",
            topic="需求评审推进",
            content="确认需求变更范围",
            source_message_ids=["m2"],
            source_conversation_ids=["c2"],
        ),
    ]

    kept, warnings = filter_sensitive_merged_drafts(drafts, RuntimeConfig())

    assert len(kept) == 1
    assert kept[0].topic == "需求评审推进"
    assert len(warnings) == 1


def test_sensitive_filter_uses_runtime_config_keywords() -> None:
    drafts = [
        MergedEventDraft(
            date="2026-06-23",
            topic="股权方案沟通",
            content="讨论股权分配",
            source_message_ids=["m1"],
            source_conversation_ids=["c1"],
        )
    ]

    config = RuntimeConfig(
        confidential_event_keywords=("股权",),
        non_work_sensitive_keywords=(),
    )
    kept, warnings = filter_sensitive_merged_drafts(drafts, config)

    assert kept == []
    assert len(warnings) == 1
