from __future__ import annotations

from pathlib import Path

from src.worktrace.config import RuntimeConfig
from src.worktrace.constants import DailyRunStatus
from src.worktrace.factories import RuntimeDependencies
from src.worktrace.models import (
    BatchAnalysisResult,
    CrossConversationGroup,
    CrossConversationGroupResult,
    ConversationRef,
    NormalizedMessage,
    SelfIdentity,
    SourceBackedEventDraft,
)
from src.worktrace.pipeline.cross_conversation_merge import (
    consolidate_workstream_groups,
    materialize_grouped_merged_drafts,
)
from src.worktrace.runner import DailyTraceRunner
from src.worktrace.stores.markdown import MarkdownEventStore


def _draft(
    *,
    draft_id: str,
    topic: str,
    content: str,
    source_message_ids: list[str],
    source_conversation_id: str,
    source_slice_id: str,
    object_hint: str,
    retention_reason: str = "decision_made",
    retention_detail: str,
    workstream_key: str = "",
) -> SourceBackedEventDraft:
    return SourceBackedEventDraft(
        draft_id=draft_id,
        date="2026-06-22",
        topic=topic,
        content=content,
        source_message_ids=source_message_ids,
        source_conversation_id=source_conversation_id,
        source_slice_id=source_slice_id,
        confidence=0.9,
        action_label="确认",
        object_hint=object_hint,
        retention_reason=retention_reason,
        retention_detail=retention_detail,
        workstream_key=workstream_key,
    )


class MergeSource:
    def get_self_identity(self):
        return SelfIdentity(open_id="ou_self", display_name="Me", source="fake")

    def list_target_conversations(self, target_date, self_identity):
        return [
            ConversationRef(conversation_id="oc_1", conversation_name="项目群1"),
            ConversationRef(conversation_id="oc_2", conversation_name="项目群2"),
            ConversationRef(conversation_id="oc_3", conversation_name="项目群3"),
        ]

    def fetch_conversation_messages(self, target_date, conversation_ids):
        return [
            NormalizedMessage(
                conversation_id="oc_1",
                conversation_name="项目群1",
                message_id="om_1",
                sender_open_id="ou_self",
                sender_name="Me",
                send_time="2026-06-22T10:00:00+08:00",
                message_type="text",
                text="release-123 排期确认",
                reply_to_message_id=None,
                quote_message_id=None,
                links=[],
                attachments=[],
                is_system=False,
            ),
            NormalizedMessage(
                conversation_id="oc_2",
                conversation_name="项目群2",
                message_id="om_2",
                sender_open_id="ou_self",
                sender_name="Me",
                send_time="2026-06-22T11:00:00+08:00",
                message_type="text",
                text="release-123 继续沟通",
                reply_to_message_id=None,
                quote_message_id=None,
                links=[],
                attachments=[],
                is_system=False,
            ),
            NormalizedMessage(
                conversation_id="oc_3",
                conversation_name="项目群3",
                message_id="om_3",
                sender_open_id="ou_self",
                sender_name="Me",
                send_time="2026-06-22T12:00:00+08:00",
                message_type="text",
                text="contract-888 合同沟通",
                reply_to_message_id=None,
                quote_message_id=None,
                links=[],
                attachments=[],
                is_system=False,
            ),
        ]

    def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
        return []


class MergeResolver:
    def to_text(self, message):
        return message.text

    def extract_links(self, message):
        return list(message.links)

    def load_attachment_text_if_needed(self, message, attachment_ids, hint):
        return None


class MergeAnalyzer:
    def __init__(self):
        self.group_calls: list[list[str]] = []

    def analyze_batch(self, target_date, batch_input):
        message_id = batch_input.slices[0].messages[0].message_id
        if message_id == "om_1":
            return BatchAnalysisResult(
                candidate_events=[
                    _draft(
                        draft_id="d1",
                        topic="发布排期确认",
                        content="同步 release-123",
                        source_message_ids=["om_1"],
                        source_conversation_id="oc_1",
                        source_slice_id=batch_input.slices[0].slice_id,
                        object_hint="release-123排期",
                        retention_detail="同步 release-123 的排期确认信息。",
                        workstream_key="release-123",
                    )
                ],
                context_requests=[],
            )
        if message_id == "om_2":
            return BatchAnalysisResult(
                candidate_events=[
                    _draft(
                        draft_id="d2",
                        topic="发布排期确认",
                        content="继续确认 release-123，已确认",
                        source_message_ids=["om_2"],
                        source_conversation_id="oc_2",
                        source_slice_id=batch_input.slices[0].slice_id,
                        object_hint="release-123排期",
                        retention_detail="继续确认 release-123 排期并形成已确认结果。",
                        workstream_key="release-123",
                    )
                ],
                context_requests=[],
            )
        return BatchAnalysisResult(
            candidate_events=[
                _draft(
                    draft_id="d3",
                    topic="合同沟通",
                    content="跟进 contract-888",
                    source_message_ids=["om_3"],
                    source_conversation_id="oc_3",
                    source_slice_id=batch_input.slices[0].slice_id,
                    object_hint="contract-888合同",
                    retention_reason="external_business_progress",
                    retention_detail="跟进 contract-888 合同事项。",
                )
            ],
            context_requests=[],
        )

    def merge_day_candidates(self, target_date, candidates):
        self.group_calls.append([item.draft_id for item in candidates])
        return CrossConversationGroupResult(
            groups=[
                CrossConversationGroup(
                    group_id="g1",
                    draft_ids=["d1", "d2"],
                    primary_draft_id="d1",
                ),
                CrossConversationGroup(
                    group_id="g2",
                    draft_ids=["d3"],
                    primary_draft_id="d3",
                ),
            ]
        )

    def build_batch_prompt(self, batch_input):
        return "prompt"

    def build_merge_prompt(self, target_date, candidates):
        return "merge prompt"


class MergeDelivery:
    def deliver_to_self(self, *, self_identity, markdown_path):
        return ("success", self_identity.open_id)


class SemanticResolutionMergeAnalyzer(MergeAnalyzer):
    def __init__(self):
        super().__init__()
        self.workstream_resolution_calls = 0

    def request_json(self, prompt, *, output_schema):
        self.workstream_resolution_calls += 1
        assert "parent_draft_id" in prompt
        assert output_schema["required"] == ["assignments"]
        if "unassigned_candidates" in prompt:
            return {
                "assignments": [
                    {
                        "draft_id": "d3",
                        "parent_draft_id": "",
                        "root_workstream_name": "",
                        "evidence_message_ids": [],
                    }
                ]
            }
        return {
            "assignments": [
                {
                    "draft_id": "d1",
                    "parent_draft_id": "d1",
                    "root_workstream_name": "release-123",
                    "evidence_message_ids": [],
                },
                {
                    "draft_id": "d2",
                    "parent_draft_id": "d1",
                    "root_workstream_name": "",
                    "evidence_message_ids": ["om_1", "om_2"],
                },
                {
                    "draft_id": "d3",
                    "parent_draft_id": "",
                    "root_workstream_name": "",
                    "evidence_message_ids": [],
                },
            ]
        }


def test_materialized_project_lifecycle_uses_primary_title_and_message_order() -> None:
    root = _draft(
        draft_id="root",
        topic="换电项目启动",
        content="项目启动并分配摄像头安装任务。",
        source_message_ids=["om_later"],
        source_conversation_id="oc_1",
        source_slice_id="slice-root",
        object_hint="换电项目",
        retention_detail="明确项目启动和分支任务。",
    )
    task = _draft(
        draft_id="task",
        topic="摄像头验收",
        content="确认摄像头验收标准。",
        source_message_ids=["om_earlier"],
        source_conversation_id="oc_2",
        source_slice_id="slice-task",
        object_hint="摄像头验收",
        retention_detail="明确验收标准。",
    )

    drafts = materialize_grouped_merged_drafts(
        [root, task],
        [
            CrossConversationGroup(
                group_id="project",
                draft_ids=["root", "task"],
                primary_draft_id="root",
            )
        ],
        target_date="2026-06-22",
        message_order=["om_earlier", "om_later"],
    )

    assert drafts[0].topic == "换电项目启动"
    assert drafts[0].source_message_ids == ["om_earlier", "om_later"]
    assert drafts[0].content == "确认摄像头验收标准。\n\n项目启动并分配摄像头安装任务。"


def test_consolidate_workstream_groups_safely_groups_exact_named_workstreams() -> None:
    project_root = _draft(
        draft_id="project-root",
        topic="项目甲启动",
        content="启动项目甲，并安排现场摄像头安装。",
        source_message_ids=["om_1"],
        source_conversation_id="oc_1",
        source_slice_id="slice-1",
        object_hint="项目甲、现场摄像头安装",
        retention_detail="项目甲启动。",
        workstream_key="项目甲",
    )
    camera_task = _draft(
        draft_id="camera-task",
        topic="现场摄像头安装任务",
        content="明确现场摄像头安装验收标准。",
        source_message_ids=["om_2"],
        source_conversation_id="oc_2",
        source_slice_id="slice-2",
        object_hint="现场摄像头安装",
        retention_detail="明确现场摄像头安装任务。",
    )
    project_monitoring = _draft(
        draft_id="project-monitoring",
        topic="项目甲监控",
        content="建立项目甲的监控统计。",
        source_message_ids=["om_3"],
        source_conversation_id="oc_3",
        source_slice_id="slice-3",
        object_hint="项目甲监控",
        retention_detail="建立项目甲监控。",
        workstream_key="项目甲",
    )
    policy_notice = _draft(
        draft_id="policy-notice",
        topic="特殊奖励政策通知",
        content="下发特殊奖励政策并明确发送范围。",
        source_message_ids=["om_4"],
        source_conversation_id="oc_4",
        source_slice_id="slice-4",
        object_hint="特殊奖励政策",
        retention_detail="下发特殊奖励政策。",
        workstream_key="特殊奖励政策",
    )
    policy_feedback = _draft(
        draft_id="policy-feedback",
        topic="特殊奖励短信执行反馈",
        content="确认特殊奖励短信已发送。",
        source_message_ids=["om_5"],
        source_conversation_id="oc_5",
        source_slice_id="slice-5",
        object_hint="特殊奖励短信通知",
        retention_detail="特殊奖励政策已执行。",
    )
    unrelated = _draft(
        draft_id="unrelated",
        topic="经营数据汇报",
        content="汇报六月经营数据，并提及现场摄像头安装统计。",
        source_message_ids=["om_6"],
        source_conversation_id="oc_6",
        source_slice_id="slice-6",
        object_hint="六月经营数据",
        retention_detail="汇报经营数据。",
    )
    tool_one = _draft(
        draft_id="tool-one",
        topic="产品丙配置",
        content="配置产品丙。",
        source_message_ids=["om_7"],
        source_conversation_id="oc_7",
        source_slice_id="slice-7",
        object_hint="产品丙",
        retention_detail="配置产品丙。",
        workstream_key="产品丙",
    )
    tool_two = _draft(
        draft_id="tool-two",
        topic="产品丁开通",
        content="开通产品丁。",
        source_message_ids=["om_8"],
        source_conversation_id="oc_8",
        source_slice_id="slice-8",
        object_hint="产品丁",
        retention_detail="开通产品丁。",
        workstream_key="产品丁",
    )

    groups, warnings = consolidate_workstream_groups(
        [
            CrossConversationGroup(
                group_id="project-task",
                draft_ids=["camera-task", "unrelated"],
                primary_draft_id="camera-task",
            ),
                CrossConversationGroup(
                    group_id="project-root",
                    draft_ids=["project-root"],
                    primary_draft_id="project-root",
                ),
                CrossConversationGroup(
                    group_id="project-monitoring",
                    draft_ids=["project-monitoring"],
                    primary_draft_id="project-monitoring",
                ),
            CrossConversationGroup(
                group_id="policy-notice",
                draft_ids=["policy-notice"],
                primary_draft_id="policy-notice",
            ),
            CrossConversationGroup(
                group_id="policy-feedback",
                draft_ids=["policy-feedback"],
                primary_draft_id="policy-feedback",
            ),
            CrossConversationGroup(
                group_id="mixed-products",
                draft_ids=["tool-one", "tool-two"],
                primary_draft_id="tool-one",
            ),
        ],
        [
            project_root,
            camera_task,
            project_monitoring,
            policy_notice,
            policy_feedback,
            unrelated,
            tool_one,
            tool_two,
        ],
    )

    group_by_ids = {frozenset(item.draft_ids): item for item in groups}

    assert frozenset({"project-root", "project-monitoring"}) in group_by_ids
    assert group_by_ids[frozenset({"project-root", "project-monitoring"})].primary_draft_id == "project-root"
    assert frozenset({"camera-task"}) in group_by_ids
    assert frozenset({"policy-notice"}) in group_by_ids
    assert frozenset({"policy-feedback"}) in group_by_ids
    assert frozenset({"unrelated"}) in group_by_ids
    assert frozenset({"tool-one"}) in group_by_ids
    assert frozenset({"tool-two"}) in group_by_ids
    assert warnings == []


def test_runner_groups_candidates_across_conversations_once(tmp_path: Path) -> None:
    analyzer = MergeAnalyzer()
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        conversation_debug_root=tmp_path / "debug",
    )
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=MergeSource(),
            content_resolver=MergeResolver(),
            analyzer=analyzer,
            delivery_channel=MergeDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert result.event_count == 2
    assert analyzer.group_calls == [["d1", "d2", "d3"]]
    merge_dir = tmp_path / "debug" / "2026-06-22" / "_merge_day_candidates"
    assert (merge_dir / "input.json").exists()
    assert (merge_dir / "prompt.txt").read_text(encoding="utf-8") == "merge prompt"


def test_runner_uses_llm_workstream_resolution_and_dumps_evidence(tmp_path: Path) -> None:
    analyzer = SemanticResolutionMergeAnalyzer()
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        conversation_debug_root=tmp_path / "debug",
    )
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=MergeSource(),
            content_resolver=MergeResolver(),
            analyzer=analyzer,
            delivery_channel=MergeDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert result.event_count == 2
    assert analyzer.workstream_resolution_calls == 2
    merge_dir = tmp_path / "debug" / "2026-06-22" / "_merge_day_candidates"
    assert (merge_dir / "workstream_resolution_input.json").exists()
    assert (merge_dir / "workstream_resolution_prompt.txt").exists()
    assert (merge_dir / "workstream_resolution_output.json").exists()
    assert (merge_dir / "workstream_resolution_validated.json").exists()
    assert (merge_dir / "workstream_resolution_followup_input.json").exists()
    assert (merge_dir / "workstream_resolution_followup_output.json").exists()


class MissingDraftMergeAnalyzer(MergeAnalyzer):
    def merge_day_candidates(self, target_date, candidates):
        self.group_calls.append([item.draft_id for item in candidates])
        return CrossConversationGroupResult(
            groups=[
                CrossConversationGroup(group_id="g1", draft_ids=["d1", "d2"]),
            ]
        )


def test_runner_recovers_when_merge_result_drops_candidate_draft(tmp_path: Path) -> None:
    analyzer = MissingDraftMergeAnalyzer()
    config = RuntimeConfig(data_root=tmp_path / "data")
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=MergeSource(),
            content_resolver=MergeResolver(),
            analyzer=analyzer,
            delivery_channel=MergeDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.SUCCESS_WITH_WARNINGS.value
    assert result.event_count == 2
    assert "Cross-conversation merge groups were repaired" in result.error_summary
    assert "missing=['d3']" in result.error_summary
