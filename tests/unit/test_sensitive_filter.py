from __future__ import annotations

from src.worktrace.config import RuntimeConfig
from src.worktrace.models import MergedEventDraft, SourceBackedEventDraft
from src.worktrace.pipeline.sensitive_filter import (
    filter_excluded_candidate_drafts,
    filter_sensitive_merged_drafts,
)


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


def test_sensitive_filter_removes_excluded_operational_noise_drafts() -> None:
    drafts = [
        MergedEventDraft(
            date="2026-06-23",
            topic="代码同步",
            content="执行 git pull 操作，可能涉及代码更新同步。",
            source_message_ids=["m1"],
            source_conversation_ids=["c1"],
        ),
        MergedEventDraft(
            date="2026-06-23",
            topic="工作面谈安排",
            content="通知同事到公司找自己，并提及聆听大老板电话。",
            source_message_ids=["m2"],
            source_conversation_ids=["c2"],
        ),
        MergedEventDraft(
            date="2026-06-23",
            topic="故障数据同步",
            content="要求提供本周发给哈尔滨的故障数据。",
            source_message_ids=["m3"],
            source_conversation_ids=["c3"],
        ),
        MergedEventDraft(
            date="2026-06-23",
            topic="需求评审推进",
            content="确认需求变更范围和上线排期。",
            source_message_ids=["m4"],
            source_conversation_ids=["c4"],
        ),
    ]

    kept, warnings = filter_sensitive_merged_drafts(drafts, RuntimeConfig())

    assert [draft.topic for draft in kept] == ["需求评审推进"]
    assert len(warnings) == 3


def test_excluded_candidate_filter_removes_only_exact_topics() -> None:
    drafts = [
        SourceBackedEventDraft(
            draft_id="d1",
            date="2026-06-23",
            topic="代码同步",
            content="执行 git pull 操作，可能涉及代码更新同步。",
            source_message_ids=["m1"],
            source_conversation_id="c1",
            source_slice_id="s1",
            confidence=0.9,
        ),
        SourceBackedEventDraft(
            draft_id="d2",
            date="2026-06-23",
            topic="索取故障数据",
            content="请补充本周全国故障数据汇总，用于后续分析。",
            source_message_ids=["m2"],
            source_conversation_id="c2",
            source_slice_id="s2",
            confidence=0.9,
        ),
    ]

    kept, warnings = filter_excluded_candidate_drafts(drafts, RuntimeConfig())

    assert [draft.topic for draft in kept] == ["索取故障数据"]
    assert len(warnings) == 1


def test_excluded_candidate_filter_removes_signature_even_when_topic_changes() -> None:
    drafts = [
        SourceBackedEventDraft(
            draft_id="d1",
            date="2026-06-23",
            topic="代码拉取",
            content="孙维晟指示执行 git pull 操作。",
            source_message_ids=["m1"],
            source_conversation_id="c1",
            source_slice_id="s1",
            confidence=0.9,
        )
    ]

    kept, warnings = filter_excluded_candidate_drafts(drafts, RuntimeConfig())

    assert kept == []
    assert len(warnings) == 1


def test_sensitive_filter_keeps_non_excluded_topic_with_different_content() -> None:
    drafts = [
        MergedEventDraft(
            date="2026-06-23",
            topic="索取故障数据",
            content="请补充本周全国故障数据汇总，用于后续分析。",
            source_message_ids=["m1"],
            source_conversation_ids=["c1"],
        )
    ]

    kept, warnings = filter_sensitive_merged_drafts(drafts, RuntimeConfig())

    assert [draft.topic for draft in kept] == ["索取故障数据"]
    assert warnings == []
