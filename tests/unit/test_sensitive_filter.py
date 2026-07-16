from __future__ import annotations

from pathlib import Path

from src.worktrace.config import RuntimeConfig, load_runtime_config_overrides
from src.worktrace.models import EventFileLink, MergedEventDraft, SourceBackedEventDraft, WorkEvent
from src.worktrace.pipeline.sensitive_filter import (
    filter_candidate_drafts,
    filter_merged_drafts,
    filter_work_events,
    filter_work_events_with_diagnostics,
)
from src.worktrace.pipeline.retention_filter import (
    filter_retained_candidate_drafts,
    filter_retained_work_events,
)


RETENTION_POLICY = load_runtime_config_overrides(
    RuntimeConfig(),
    cwd=Path.cwd(),
).retention_policy

REPO_CONFIG = load_runtime_config_overrides(
    RuntimeConfig(),
    cwd=Path.cwd(),
)


def test_merged_filter_removes_configured_sensitive_keywords() -> None:
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

    kept, warnings = filter_merged_drafts(
        drafts,
        RuntimeConfig(sensitive_event_keywords=("工资",)),
    )

    assert [draft.topic for draft in kept] == ["项目发布推进"]
    assert warnings == ["Filtered sensitive event."]


def test_merged_filter_removes_sensitive_keyword_from_retention_detail() -> None:
    drafts = [
        MergedEventDraft(
            date="2026-06-23",
            topic="团队事项",
            content="确认处理安排",
            source_message_ids=["m1"],
            source_conversation_ids=["c1"],
            retention_detail="讨论薪资调整方案。",
        ),
        MergedEventDraft(
            date="2026-06-23",
            topic="需求评审推进",
            content="确认需求变更范围",
            source_message_ids=["m2"],
            source_conversation_ids=["c2"],
        ),
    ]

    kept, warnings = filter_merged_drafts(
        drafts,
        RuntimeConfig(sensitive_event_keywords=("薪资",)),
    )

    assert [draft.topic for draft in kept] == ["需求评审推进"]
    assert warnings == ["Filtered sensitive event."]


def test_work_event_filter_removes_personnel_exit_and_recruitment_event() -> None:
    events = [
        WorkEvent(
            date="2026-07-15",
            event_id="evt-personnel",
            title="人员离职与招聘决策",
            content="员工已有外部 offer，请示是挽留还是启动招聘。",
            object_hint="员工去留",
            retention_detail="员工提出离职意向。",
        ),
        WorkEvent(
            date="2026-07-15",
            event_id="evt-keep",
            title="项目发布推进",
            content="确认上线节奏。",
        ),
    ]

    kept, warnings = filter_work_events(
        events,
        RuntimeConfig(sensitive_event_keywords=("离职", "招聘", "offer")),
    )

    assert [event.event_id for event in kept] == ["evt-keep"]
    assert warnings == ["Filtered sensitive event."]


def test_personnel_retention_compensation_is_filtered_at_all_three_layers() -> None:
    candidate = SourceBackedEventDraft(
        draft_id="evt-personnel-retention",
        date="2026-07-15",
        topic="陈某挽留谈判及报价确认",
        content="建议进行挽留流程，沟通后确认挽留报价。",
        source_message_ids=["m1"],
        source_conversation_id="c1",
        source_slice_id="s1",
        confidence=0.9,
        object_hint="陈某挽留方案",
        retention_reason="decision_made",
        retention_detail="本人提出挽留建议并询问预算。",
    )
    merged = MergedEventDraft(
        date=candidate.date,
        topic=candidate.topic,
        content=candidate.content,
        source_message_ids=candidate.source_message_ids,
        source_conversation_ids=[candidate.source_conversation_id],
        object_hint=candidate.object_hint,
        retention_reason=candidate.retention_reason,
        retention_detail=candidate.retention_detail,
    )
    event = WorkEvent(
        date=candidate.date,
        event_id=candidate.draft_id,
        title=candidate.topic,
        content=candidate.content,
        object_hint=candidate.object_hint,
        retention_reason=candidate.retention_reason,
        retention_detail=candidate.retention_detail,
    )

    candidate_kept, candidate_warnings = filter_candidate_drafts(
        [candidate], REPO_CONFIG
    )
    merged_kept, merged_warnings = filter_merged_drafts([merged], REPO_CONFIG)
    event_kept, event_warnings = filter_work_events([event], REPO_CONFIG)

    assert candidate_kept == []
    assert merged_kept == []
    assert event_kept == []
    assert candidate_warnings == ["Filtered sensitive event."]
    assert merged_warnings == ["Filtered sensitive event."]
    assert event_warnings == ["Filtered sensitive event."]


def test_merged_markdown_title_is_excluded_at_all_three_layers() -> None:
    title = "《2026-07-14-部门负责人-merged.md》日报质量分析"
    candidate = SourceBackedEventDraft(
        draft_id="evt-merged-markdown",
        date="2026-07-15",
        topic=title,
        content="检查日报文件体积并提出整改建议。",
        source_message_ids=["m1"],
        source_conversation_id="c1",
        source_slice_id="s1",
        confidence=0.9,
    )
    merged = MergedEventDraft(
        date=candidate.date,
        topic=title,
        content=candidate.content,
        source_message_ids=candidate.source_message_ids,
        source_conversation_ids=[candidate.source_conversation_id],
    )
    event = WorkEvent(
        date=candidate.date,
        event_id=candidate.draft_id,
        title=title,
        content=candidate.content,
    )

    candidate_kept, candidate_warnings = filter_candidate_drafts(
        [candidate], REPO_CONFIG
    )
    merged_kept, merged_warnings = filter_merged_drafts([merged], REPO_CONFIG)
    event_kept, event_warnings = filter_work_events([event], REPO_CONFIG)

    assert candidate_kept == []
    assert merged_kept == []
    assert event_kept == []
    assert candidate_warnings == ["Filtered excluded event."]
    assert merged_warnings == ["Filtered excluded event."]
    assert event_warnings == ["Filtered excluded event."]


def test_work_event_filter_removes_sensitive_file_link_without_leaking_title() -> None:
    events = [
        WorkEvent(
            date="2026-06-23",
            event_id="evt-1",
            title="项目发布推进",
            content="确认上线节奏",
            file_links=[
                EventFileLink(
                    url="https://example.com/docs/1",
                    title="薪资调整说明",
                    link_type="doc",
                )
            ],
        )
    ]

    kept, warnings = filter_work_events(
        events,
        RuntimeConfig(sensitive_event_keywords=("薪资",)),
    )

    assert kept == []
    assert warnings == ["Filtered sensitive event."]


def test_work_event_filter_returns_structured_diagnostics() -> None:
    events = [
        WorkEvent(
            date="2026-06-29",
            event_id="evt-sensitive",
            title="薪资调整",
            content="确认薪资调整方案。",
        ),
        WorkEvent(
            date="2026-06-29",
            event_id="evt-excluded",
            title="测试事件",
            content="执行 git pull。",
        ),
        WorkEvent(
            date="2026-06-29",
            event_id="evt-keep",
            title="项目排期",
            content="确认项目排期。",
        ),
    ]

    kept, diagnostics = filter_work_events_with_diagnostics(
        events,
        RuntimeConfig(
            sensitive_event_keywords=("薪资",),
            excluded_event_keywords=("git pull",),
        ),
    )

    assert [event.event_id for event in kept] == ["evt-keep"]
    assert [(item.item_index, item.kind) for item in diagnostics] == [
        (0, "sensitive"),
        (1, "excluded"),
    ]


def test_merged_filter_removes_excluded_operational_noise_drafts() -> None:
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
    config = RuntimeConfig(
        excluded_event_keywords=("代码同步", "聆听大老板电话", "故障数据同步"),
    )

    kept, warnings = filter_merged_drafts(drafts, config)

    assert [draft.topic for draft in kept] == ["需求评审推进"]
    assert len(warnings) == 3


def test_excluded_candidate_filter_removes_keyword_from_object_hint() -> None:
    drafts = [
        SourceBackedEventDraft(
            draft_id="d1",
            date="2026-06-23",
            topic="常规同步",
            content="确认进展。",
            source_message_ids=["m1"],
            source_conversation_id="c1",
            source_slice_id="s1",
            confidence=0.9,
            object_hint="代码同步",
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

    config = RuntimeConfig(excluded_event_keywords=("代码同步",))
    kept, warnings = filter_candidate_drafts(drafts, config)

    assert [draft.topic for draft in kept] == ["索取故障数据"]
    assert len(warnings) == 1


def test_excluded_candidate_filter_removes_keyword_from_content() -> None:
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

    config = RuntimeConfig(excluded_event_keywords=("git pull",))
    kept, warnings = filter_candidate_drafts(drafts, config)

    assert kept == []
    assert len(warnings) == 1


def test_merged_filter_removes_signature_even_when_topic_changes() -> None:
    drafts = [
        MergedEventDraft(
            date="2026-06-23",
            topic="代码拉取",
            content="孙维晟指示执行 git pull 操作。",
            source_message_ids=["m1"],
            source_conversation_ids=["c1"],
        ),
        MergedEventDraft(
            date="2026-06-23",
            topic="需求评审推进",
            content="确认需求变更范围和上线排期。",
            source_message_ids=["m2"],
            source_conversation_ids=["c2"],
        ),
    ]
    config = RuntimeConfig(excluded_event_keywords=("git pull",))

    kept, warnings = filter_merged_drafts(drafts, config)

    assert [draft.topic for draft in kept] == ["需求评审推进"]
    assert len(warnings) == 1


def test_merged_filter_keeps_non_excluded_topic_with_different_content() -> None:
    drafts = [
        MergedEventDraft(
            date="2026-06-23",
            topic="索取故障数据",
            content="请补充本周全国故障数据汇总，用于后续分析。",
            source_message_ids=["m1"],
            source_conversation_ids=["c1"],
        )
    ]

    kept, warnings = filter_merged_drafts(drafts, RuntimeConfig())

    assert [draft.topic for draft in kept] == ["索取故障数据"]
    assert warnings == []


def test_retention_filter_removes_generic_review_completion() -> None:
    drafts = [
        SourceBackedEventDraft(
            draft_id="d1",
            date="2026-06-29",
            topic="完成审核",
            content="完成审核工作。",
            source_message_ids=["m1"],
            source_conversation_id="c1",
            source_slice_id="s1",
            confidence=0.9,
            action_label="审核",
            object_hint="审核",
            retention_reason="substantive_approval",
            retention_detail="完成审核工作。",
        )
    ]

    kept, warnings = filter_retained_candidate_drafts(drafts, RETENTION_POLICY)

    assert kept == []
    assert len(warnings) == 1


def test_retention_filter_removes_personal_social_reputation_event() -> None:
    drafts = [
        SourceBackedEventDraft(
            draft_id="d1",
            date="2026-06-30",
            topic="团队口碑反馈",
            content=(
                "梁媛媛提到产品团队对其口碑评价良好，并约定今晚在公司旁边"
                "吃辣味牛蛙火锅，饭后直接坐地铁回去准备述职报告材料。"
            ),
            source_message_ids=["m1"],
            source_conversation_id="c1",
            source_slice_id="s1",
            confidence=0.9,
            action_label="跟进",
            object_hint="团队口碑反馈",
            retention_reason="follow_up_assigned",
            retention_detail="确定今晚与公司同事梁媛媛在公司附近吃辣味牛蛙火锅。",
        )
    ]

    kept, warnings = filter_retained_candidate_drafts(drafts, RETENTION_POLICY)

    assert kept == []
    assert len(warnings) == 1
    assert "personal_social_or_reputation_event" in warnings[0]


def test_retention_filter_removes_personal_privacy_leave_event() -> None:
    drafts = [
        SourceBackedEventDraft(
            draft_id="d1",
            date="2026-06-30",
            topic="明日行程报备",
            content="本人明天上午晚到，需前往学校为孩子开具相关证明。",
            source_message_ids=["m1"],
            source_conversation_id="c1",
            source_slice_id="s1",
            confidence=0.9,
            action_label="报备",
            object_hint="个人请假/外出事由",
            retention_reason="follow_up_assigned",
            retention_detail="本人告知老板及同事次日行程安排及原因。",
        )
    ]

    kept, warnings = filter_retained_candidate_drafts(drafts, RETENTION_POLICY)

    assert kept == []
    assert len(warnings) == 1
    assert "personal_privacy_or_leave_event" in warnings[0]


def test_retention_filter_removes_personal_privacy_leave_work_event() -> None:
    events = [
        WorkEvent(
            date="2026-06-30",
            event_id="evt1",
            title="明日行程报备",
            content="本人明天上午晚到，需前往学校为孩子开具相关证明。",
            object_hint="个人请假/外出事由",
            retention_reason="follow_up_assigned",
            retention_detail="本人告知老板及同事次日行程安排及原因。",
            source_message_ids=["m1"],
        )
    ]

    kept, warnings = filter_retained_work_events(events, RETENTION_POLICY)

    assert kept == []
    assert len(warnings) == 1
    assert "personal_privacy_or_leave_event" in warnings[0]


def test_retention_filter_keeps_business_site_visit_event() -> None:
    drafts = [
        SourceBackedEventDraft(
            draft_id="d1",
            date="2026-06-30",
            topic="客户现场设备验收",
            content="明日去客户现场完成设备验收，并同步验收结论。",
            source_message_ids=["m1"],
            source_conversation_id="c1",
            source_slice_id="s1",
            confidence=0.9,
            action_label="验收",
            object_hint="客户现场设备验收",
            retention_reason="external_business_progress",
            retention_detail="确认明日到客户现场完成设备验收并同步结论。",
        )
    ]

    kept, warnings = filter_retained_candidate_drafts(drafts, RETENTION_POLICY)

    assert [draft.topic for draft in kept] == ["客户现场设备验收"]
    assert warnings == []


def test_retention_filter_removes_generic_review_with_named_submitter() -> None:
    drafts = [
        SourceBackedEventDraft(
            draft_id="d1",
            date="2026-06-29",
            topic="完成工作审核",
            content="孙维晟完成了郭海提交的工作审核，并同步了审核结果。",
            source_message_ids=["m1"],
            source_conversation_id="c1",
            source_slice_id="s1",
            confidence=0.9,
            action_label="审核",
            object_hint="工作审核",
            retention_reason="substantive_approval",
            retention_detail=(
                "2026-06-29 12:30 孙维晟完成审核任务，"
                "该事项涉及具体业务审批动作的闭环。"
            ),
        )
    ]

    kept, warnings = filter_retained_candidate_drafts(drafts, RETENTION_POLICY)

    assert kept == []
    assert len(warnings) == 1
    assert "generic_review_completion" in warnings[0]


def test_retention_filter_removes_overtime_approval() -> None:
    drafts = [
        SourceBackedEventDraft(
            draft_id="d1",
            date="2026-07-06",
            topic="高建星6月加班审批",
            content="已审批高建星6月份的加班申请。",
            source_message_ids=["m1"],
            source_conversation_id="c1",
            source_slice_id="s1",
            confidence=0.9,
            action_label="审批",
            object_hint="高建星6月加班审批",
            retention_reason="substantive_approval",
            retention_detail="郎晓妹提醒待审批高建星6月加班，孙维晟回复确认并执行审批。",
        )
    ]

    kept, warnings = filter_retained_candidate_drafts(drafts, RETENTION_POLICY)

    assert kept == []
    assert len(warnings) == 1
    assert "administrative_approval_event" in warnings[0]


def test_retention_filter_removes_leave_or_attendance_approvals() -> None:
    drafts = [
        SourceBackedEventDraft(
            draft_id="d1",
            date="2026-07-06",
            topic="请假审批",
            content="审批请假申请。",
            source_message_ids=["m1"],
            source_conversation_id="c1",
            source_slice_id="s1",
            confidence=0.9,
            action_label="审批",
            object_hint="请假申请",
            retention_reason="substantive_approval",
            retention_detail="完成请假审批流程。",
        ),
        SourceBackedEventDraft(
            draft_id="d2",
            date="2026-07-06",
            topic="补卡审批",
            content="完成补卡审批。",
            source_message_ids=["m2"],
            source_conversation_id="c1",
            source_slice_id="s1",
            confidence=0.9,
            action_label="审批",
            object_hint="补卡申请",
            retention_reason="substantive_approval",
            retention_detail="完成补卡流程审批。",
        ),
        SourceBackedEventDraft(
            draft_id="d3",
            date="2026-07-06",
            topic="外出报备审批",
            content="确认外出报备流程。",
            source_message_ids=["m3"],
            source_conversation_id="c1",
            source_slice_id="s1",
            confidence=0.9,
            action_label="审批",
            object_hint="外出报备",
            retention_reason="substantive_approval",
            retention_detail="完成外出报备审批。",
        ),
    ]

    kept, warnings = filter_retained_candidate_drafts(drafts, RETENTION_POLICY)

    assert kept == []
    assert len(warnings) == 3
    assert all("administrative_approval_event" in warning for warning in warnings)


def test_retention_filter_keeps_substantive_approval() -> None:
    drafts = [
        SourceBackedEventDraft(
            draft_id="d1",
            date="2026-06-29",
            topic="合同审核",
            content="审核客户合同并反馈付款条款问题。",
            source_message_ids=["m1"],
            source_conversation_id="c1",
            source_slice_id="s1",
            confidence=0.9,
            action_label="审核",
            object_hint="客户合同付款条款",
            retention_reason="substantive_approval",
            retention_detail="反馈客户合同中的付款条款问题。",
        )
    ]

    kept, warnings = filter_retained_candidate_drafts(drafts, RETENTION_POLICY)

    assert [draft.topic for draft in kept] == ["合同审核"]
    assert warnings == []


def test_retention_filter_keeps_specific_payment_approval_rejection() -> None:
    drafts = [
        SourceBackedEventDraft(
            draft_id="d1",
            date="2026-06-29",
            topic="项目付款审批",
            content="审批某项目付款申请，并驳回缺少发票附件的问题。",
            source_message_ids=["m1"],
            source_conversation_id="c1",
            source_slice_id="s1",
            confidence=0.9,
            action_label="审批",
            object_hint="项目付款申请",
            retention_reason="substantive_approval",
            retention_detail="驳回某项目付款申请中缺少发票附件的问题。",
        )
    ]

    kept, warnings = filter_retained_candidate_drafts(drafts, RETENTION_POLICY)

    assert [draft.topic for draft in kept] == ["项目付款审批"]
    assert warnings == []


def test_retention_filter_removes_event_missing_metadata_even_with_file_link() -> None:
    from src.worktrace.models import EventFileLink, WorkEvent

    events = [
        WorkEvent(
            date="2026-06-29",
            event_id="evt1",
            title="下午会议安排",
            content="确认下午2点开会互通信息。",
            file_links=[
                EventFileLink(
                    url="https://example.com/doc",
                    title="会议文档",
                    link_type="url",
                )
            ],
        )
    ]

    kept, warnings = filter_retained_work_events(events, RETENTION_POLICY)

    assert kept == []
    assert len(warnings) == 1
