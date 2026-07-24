from __future__ import annotations

import json
from pathlib import Path

from src.worktrace.config import RuntimeConfig
from src.worktrace.constants import DailyRunStatus
from src.worktrace.factories import RuntimeDependencies
from src.worktrace.llm_usage import LLMUsageRecorder
from src.worktrace.models import (
    BatchAnalysisResult,
    CrossConversationGroup,
    CrossConversationGroupResult,
    ConversationRef,
    NormalizedMessage,
    SelfIdentity,
    SourceBackedEventDraft,
)
from src.worktrace.runner import DailyTraceRunner
from src.worktrace.stores.markdown import MarkdownEventStore


class FakeSource:
    def get_self_identity(self):
        return SelfIdentity(open_id="ou_self", display_name="Me", source="fake")

    def list_target_conversations(self, target_date, self_identity):
        return [ConversationRef(conversation_id="oc_1", conversation_name="项目群")]

    def fetch_conversation_messages(self, target_date, conversation_ids):
        return [
            NormalizedMessage(
                conversation_id="oc_1",
                conversation_name="项目群",
                message_id="om_1",
                sender_open_id="ou_self",
                sender_name="Me",
                send_time="2026-06-22T10:00:00+08:00",
                message_type="text",
                text="推进发布",
                reply_to_message_id=None,
                quote_message_id=None,
                links=[],
                attachments=[],
                is_system=False,
            )
        ]

    def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
        return []


class FakeResolver:
    def to_text(self, message):
        return message.text

    def extract_links(self, message):
        return list(message.links)

    def load_attachment_text_if_needed(self, message, attachment_ids, hint):
        return None


def _draft(
    *,
    draft_id: str,
    date: str = "2026-06-22",
    topic: str,
    content: str,
    source_message_ids: list[str],
    source_conversation_id: str = "oc_1",
    source_slice_id: str,
    object_hint: str | None = None,
    retention_reason: str = "decision_made",
    retention_detail: str | None = None,
) -> SourceBackedEventDraft:
    return SourceBackedEventDraft(
        draft_id=draft_id,
        date=date,
        topic=topic,
        content=content,
        source_message_ids=source_message_ids,
        source_conversation_id=source_conversation_id,
        source_slice_id=source_slice_id,
        confidence=0.9,
        action_label="确认",
        object_hint=object_hint or f"{topic}对象",
        retention_reason=retention_reason,
        retention_detail=retention_detail or f"确认{topic}的具体结果和后续安排。",
    )


class FakeAnalyzer:
    def build_batch_prompt(self, batch_input):
        return "batch prompt"

    def analyze_batch(self, target_date, batch_input):
        return BatchAnalysisResult(
            candidate_events=[
                _draft(
                    draft_id="draft-1",
                    topic="发布推进",
                    content="完成发布沟通",
                    source_message_ids=["om_1"],
                    source_slice_id=batch_input.slices[0].slice_id,
                    object_hint="发布上线窗口",
                    retention_reason="follow_up_assigned",
                    retention_detail="确认发布沟通后的上线窗口安排。",
                )
            ],
            context_requests=[],
        )

    def merge_day_candidates(self, target_date, candidates, *, validation_feedback=""):
        raise AssertionError("Should not group when there is only one candidate")


class FakeDelivery:
    def deliver_to_self(self, *, self_identity, markdown_path):
        return ("success", self_identity.open_id)


def test_runner_happy_path(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        excluded_event_keywords=("代码同步", "git pull"),
    )
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=FakeSource(),
            content_resolver=FakeResolver(),
            analyzer=FakeAnalyzer(),
            delivery_channel=FakeDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert result.event_count == 1
    assert result.output_path is not None
    assert Path(result.output_path).name == "2026-06-22-Me.md"
    assert result.self_delivery_status == "success"
    assert result.self_delivery_target == "ou_self"
    assert not (tmp_path / "data" / "debug" / "conversations").exists()


def test_runner_includes_image_failure_in_final_warnings(tmp_path: Path) -> None:
    class ImageWarningResolver(FakeResolver):
        def __init__(self) -> None:
            self.warning_messages = [
                "Skipped image summary for message om_image: 413 response"
            ]

        def drain_warning_messages(self):
            warnings = list(self.warning_messages)
            self.warning_messages.clear()
            return warnings

    config = RuntimeConfig(data_root=tmp_path / "data")
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=FakeSource(),
            content_resolver=ImageWarningResolver(),
            analyzer=FakeAnalyzer(),
            delivery_channel=FakeDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.SUCCESS_WITH_WARNINGS.value
    assert result.warning_count == 1
    assert result.error_summary == (
        "Skipped image summary for message om_image: 413 response"
    )


def test_runner_dumps_first_pass_conversation_debug_artifacts(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        conversation_debug_root=tmp_path / "debug",
    )
    usage_recorder = LLMUsageRecorder()
    usage_recorder.record(
        "segment_batch_analysis",
        {"usage": {"output_tokens": 23}},
    )
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=FakeSource(),
            content_resolver=FakeResolver(),
            analyzer=FakeAnalyzer(),
            delivery_channel=FakeDelivery(),
            event_store=MarkdownEventStore(config=config),
            llm_usage_recorder=usage_recorder,
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.SUCCESS.value
    pass_dir = tmp_path / "debug" / "2026-06-22" / "oc_1__om_1" / "pass_01"
    assert (pass_dir / "input.json").exists()
    assert (pass_dir / "prompt.txt").read_text(encoding="utf-8") == "batch prompt"
    assert (pass_dir / "output.json").exists()
    meta = (pass_dir / "meta.json").read_text(encoding="utf-8")
    assert '"status": "completed"' in meta
    assert '"candidate_event_count": 1' in meta
    final_path = tmp_path / "debug" / "2026-06-22" / "final_events.json"
    final_payload = json.loads(final_path.read_text(encoding="utf-8"))
    assert final_payload["target_date"] == "2026-06-22"
    assert len(final_payload["merged_drafts"]) == 1
    assert len(final_payload["events"]) == 1
    assert set(
        (
            "action_labels",
            "self_relations",
            "evidence_fingerprints",
            "file_keys",
        )
    ).issubset(final_payload["events"][0])
    assert len(final_payload["events"][0]["evidence_fingerprints"]) == 1
    assert final_payload["warnings"] == {
        "event_build": [],
        "final_filter": [],
        "retention": [],
    }
    usage_payload = json.loads(
        (tmp_path / "debug" / "2026-06-22" / "llm_usage.json").read_text(
            encoding="utf-8"
        )
    )
    assert usage_payload["usage"]["output_tokens"] == 23
    assert usage_payload["usage"]["missing_output_tokens_request_count"] == 0
    assert usage_payload["requests"][0]["request_kind"] == "segment_batch_analysis"


def test_runner_groups_multiple_self_messages_in_same_conversation_into_one_llm_call(
    tmp_path: Path,
) -> None:
    class MultiAnchorSource(FakeSource):
        def fetch_conversation_messages(self, target_date, conversation_ids):
            return [
                NormalizedMessage(
                    conversation_id="oc_1",
                    conversation_name="项目群",
                    message_id="om_1",
                    sender_open_id="ou_self",
                    sender_name="Me",
                    send_time="2026-06-22T10:00:00+08:00",
                    message_type="text",
                    text="推进发布",
                    reply_to_message_id=None,
                    quote_message_id=None,
                    links=[],
                    attachments=[],
                    is_system=False,
                ),
                NormalizedMessage(
                    conversation_id="oc_1",
                    conversation_name="项目群",
                    message_id="om_2",
                    sender_open_id="ou_other",
                    sender_name="Alice",
                    send_time="2026-06-22T10:01:00+08:00",
                    message_type="text",
                    text="收到",
                    reply_to_message_id="om_1",
                    quote_message_id=None,
                    links=[],
                    attachments=[],
                    is_system=False,
                ),
                NormalizedMessage(
                    conversation_id="oc_1",
                    conversation_name="项目群",
                    message_id="om_3",
                    sender_open_id="ou_self",
                    sender_name="Me",
                    send_time="2026-06-22T10:02:00+08:00",
                    message_type="text",
                    text="补充上线窗口",
                    reply_to_message_id=None,
                    quote_message_id=None,
                    links=[],
                    attachments=[],
                    is_system=False,
                ),
            ]

    class PerConversationAnalyzer(FakeAnalyzer):
        def __init__(self):
            self.batch_calls = 0
            self.slice_counts: list[int] = []

        def analyze_batch(self, target_date, batch_input):
            self.batch_calls += 1
            self.slice_counts.append(len(batch_input.slices))
            return BatchAnalysisResult(
                candidate_events=[
                    _draft(
                        draft_id="draft-1",
                        topic="发布推进",
                        content="完成发布沟通",
                        source_message_ids=["om_1", "om_3"],
                        source_slice_id=batch_input.slices[0].slice_id,
                        object_hint="发布上线窗口",
                        retention_reason="follow_up_assigned",
                        retention_detail="补充并确认发布上线窗口安排。",
                    )
                ],
                context_requests=[],
            )

        def merge_day_candidates(self, target_date, candidates, *, validation_feedback=""):
            raise AssertionError("Should not group when there is only one candidate")

    analyzer = PerConversationAnalyzer()
    config = RuntimeConfig(data_root=tmp_path / "data", anchor_batch_size=3)
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=MultiAnchorSource(),
            content_resolver=FakeResolver(),
            analyzer=analyzer,
            delivery_channel=FakeDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert analyzer.batch_calls == 1
    assert analyzer.slice_counts == [1]


def test_runner_keeps_distinct_events_with_same_source_message_ids_separate(
    tmp_path: Path,
) -> None:
    class SameSourceMultiEventAnalyzer(FakeAnalyzer):
        def analyze_batch(self, target_date, batch_input):
            return BatchAnalysisResult(
                candidate_events=[
                    _draft(
                        draft_id="draft-1",
                        topic="索取全国故障汇总",
                        content="要求获取本周全国故障汇总。",
                        source_message_ids=["om_1"],
                        source_slice_id=batch_input.slices[0].slice_id,
                        object_hint="全国故障汇总",
                        retention_reason="deliverable_updated",
                        retention_detail="要求补充本周全国故障汇总数据。",
                    ),
                    _draft(
                        draft_id="draft-2",
                        topic="权限重置确认",
                        content="郭海重置了被封的权限/账号，并确认已知晓。",
                        source_message_ids=["om_1"],
                        source_slice_id=batch_input.slices[0].slice_id,
                        object_hint="被封权限账号",
                        retention_reason="issue_or_risk_found",
                        retention_detail="确认被封权限账号已由郭海重置并知晓。",
                    ),
                ],
                context_requests=[],
            )

        def merge_day_candidates(self, target_date, candidates, *, validation_feedback=""):
            return CrossConversationGroupResult(
                groups=[
                        CrossConversationGroup(
                            group_id="g1",
                            draft_ids=["draft-1"],
                            primary_draft_id="draft-1",
                        ),
                        CrossConversationGroup(
                            group_id="g2",
                            draft_ids=["draft-2"],
                            primary_draft_id="draft-2",
                        ),
                ]
            )

    config = RuntimeConfig(
        data_root=tmp_path / "data",
        excluded_event_keywords=("代码同步", "git pull"),
    )
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=FakeSource(),
            content_resolver=FakeResolver(),
            analyzer=SameSourceMultiEventAnalyzer(),
            delivery_channel=FakeDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert result.event_count == 2
    assert result.output_path is not None

    content = Path(result.output_path).read_text(encoding="utf-8")
    assert "### 1. 索取全国故障汇总" in content
    assert "### 2. 权限重置确认" in content
    assert "要求获取本周全国故障汇总。" in content
    assert "郭海重置了被封的权限/账号，并确认已知晓。" in content


def test_runner_excludes_configured_topics_before_merge(tmp_path: Path) -> None:
    class ExcludedTopicAnalyzer(FakeAnalyzer):
        def analyze_batch(self, target_date, batch_input):
            return BatchAnalysisResult(
                candidate_events=[
                    _draft(
                        draft_id="draft-1",
                        topic="代码同步",
                        content="执行 git pull 操作，可能涉及代码更新同步。",
                        source_message_ids=["om_1"],
                        source_slice_id=batch_input.slices[0].slice_id,
                        object_hint="代码同步",
                        retention_reason="deliverable_updated",
                        retention_detail="执行 git pull 更新代码内容。",
                    ),
                    _draft(
                        draft_id="draft-2",
                        topic="权限重置确认",
                        content="郭海重置了被封的权限/账号，并确认已知晓。",
                        source_message_ids=["om_1"],
                        source_slice_id=batch_input.slices[0].slice_id,
                        object_hint="被封权限账号",
                        retention_reason="issue_or_risk_found",
                        retention_detail="确认被封权限账号已由郭海重置并知晓。",
                    ),
                ],
                context_requests=[],
            )

        def merge_day_candidates(self, target_date, candidates, *, validation_feedback=""):
            raise AssertionError("Excluded topics should be filtered before merge")

    config = RuntimeConfig(
        data_root=tmp_path / "data",
        excluded_event_keywords=("代码同步", "git pull"),
    )
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=FakeSource(),
            content_resolver=FakeResolver(),
            analyzer=ExcludedTopicAnalyzer(),
            delivery_channel=FakeDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.SUCCESS_WITH_WARNINGS.value
    assert result.event_count == 1
    assert result.output_path is not None

    content = Path(result.output_path).read_text(encoding="utf-8")
    assert "### 1. 权限重置确认" in content
    assert "代码同步" not in content
    assert "执行 git pull 操作" not in content


def test_runner_filters_low_retention_events_before_merge(tmp_path: Path) -> None:
    class LowRetentionAnalyzer(FakeAnalyzer):
        def analyze_batch(self, target_date, batch_input):
            return BatchAnalysisResult(
                candidate_events=[
                    _draft(
                        draft_id="draft-1",
                        topic="完成审核",
                        content="完成审核工作。",
                        source_message_ids=["om_1"],
                        source_slice_id=batch_input.slices[0].slice_id,
                        object_hint="审核",
                        retention_reason="substantive_approval",
                        retention_detail="完成审核工作。",
                    ),
                    _draft(
                        draft_id="draft-2",
                        topic="合同审核",
                        content="审核客户合同并反馈付款条款问题。",
                        source_message_ids=["om_1"],
                        source_slice_id=batch_input.slices[0].slice_id,
                        object_hint="客户合同付款条款",
                        retention_reason="substantive_approval",
                        retention_detail="反馈客户合同付款条款问题。",
                    ),
                ],
                context_requests=[],
            )

        def merge_day_candidates(self, target_date, candidates, *, validation_feedback=""):
            raise AssertionError("Only one retained candidate should skip merge")

    config = RuntimeConfig(data_root=tmp_path / "data")
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=FakeSource(),
            content_resolver=FakeResolver(),
            analyzer=LowRetentionAnalyzer(),
            delivery_channel=FakeDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.SUCCESS_WITH_WARNINGS.value
    assert result.event_count == 1
    assert result.output_path is not None
    content = Path(result.output_path).read_text(encoding="utf-8")
    assert "### 1. 合同审核" in content
    assert "完成审核" not in content


def test_runner_filters_non_self_related_other_people_event_before_merge(
    tmp_path: Path,
) -> None:
    class OtherPeopleDiscussionSource(FakeSource):
        def get_self_identity(self):
            return SelfIdentity(open_id="ou_self", display_name="张玉环", source="fake")

        def fetch_conversation_messages(self, target_date, conversation_ids):
            return [
                NormalizedMessage(
                    conversation_id="oc_1",
                    conversation_name="项目群",
                    message_id="om_1",
                    sender_open_id="ou_self",
                    sender_name="张玉环",
                    send_time="2026-06-22T10:00:00+08:00",
                    message_type="text",
                    text="我知道了",
                    reply_to_message_id=None,
                    quote_message_id=None,
                    links=[],
                    attachments=[],
                    is_system=False,
                ),
                NormalizedMessage(
                    conversation_id="oc_1",
                    conversation_name="项目群",
                    message_id="om_2",
                    sender_open_id="ou_other_1",
                    sender_name="丁金龙",
                    send_time="2026-06-22T10:01:00+08:00",
                    message_type="text",
                    text="不需要，删除吧，把所有和我有关的都删除吧",
                    reply_to_message_id=None,
                    quote_message_id=None,
                    links=[],
                    attachments=[],
                    is_system=False,
                ),
                NormalizedMessage(
                    conversation_id="oc_1",
                    conversation_name="项目群",
                    message_id="om_3",
                    sender_open_id="ou_other_2",
                    sender_name="付晨",
                    send_time="2026-06-22T10:02:00+08:00",
                    message_type="text",
                    text="那能找人帮您提个看板吧，我找开发把所有经销商的都删了",
                    reply_to_message_id=None,
                    quote_message_id=None,
                    links=[],
                    attachments=[],
                    is_system=False,
                ),
            ]

    class OtherPeopleEventAnalyzer(FakeAnalyzer):
        def analyze_batch(self, target_date, batch_input):
            return BatchAnalysisResult(
                candidate_events=[
                    _draft(
                        draft_id="draft-1",
                        topic="聊天记录清理跟进",
                        content="丁金龙要求删除与本人有关的聊天记录，付晨提出提看板处理。",
                        source_message_ids=["om_2", "om_3"],
                        source_slice_id=batch_input.slices[0].slice_id,
                        object_hint="聊天记录清理与看板处理",
                        retention_reason="follow_up_assigned",
                        retention_detail="丁金龙明确要求删除记录，付晨提出后续看板方案。",
                    )
                ],
                context_requests=[],
            )

        def merge_day_candidates(self, target_date, candidates, *, validation_feedback=""):
            raise AssertionError("Non-self-related candidates should be filtered before merge")

    config = RuntimeConfig(data_root=tmp_path / "data")
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=OtherPeopleDiscussionSource(),
            content_resolver=FakeResolver(),
            analyzer=OtherPeopleEventAnalyzer(),
            delivery_channel=FakeDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.SUCCESS_WITH_WARNINGS.value
    assert result.event_count == 0
    assert "Filtered non-self-related event draft: 聊天记录清理跟进" in result.error_summary
    assert result.output_path is not None
    content = Path(result.output_path).read_text(encoding="utf-8")
    assert "聊天记录清理跟进" not in content


def test_runner_sorts_events_by_source_message_time(tmp_path: Path) -> None:
    class OutOfOrderSource(FakeSource):
        def fetch_conversation_messages(self, target_date, conversation_ids):
            return [
                NormalizedMessage(
                    conversation_id="oc_1",
                    conversation_name="项目群",
                    message_id="om_late",
                    sender_open_id="ou_self",
                    sender_name="Me",
                    send_time="2026-06-22T11:00:00+08:00",
                    message_type="text",
                    text="较晚事件",
                    reply_to_message_id=None,
                    quote_message_id=None,
                    links=[],
                    attachments=[],
                    is_system=False,
                ),
                NormalizedMessage(
                    conversation_id="oc_1",
                    conversation_name="项目群",
                    message_id="om_early",
                    sender_open_id="ou_self",
                    sender_name="Me",
                    send_time="2026-06-22T09:00:00+08:00",
                    message_type="text",
                    text="较早事件",
                    reply_to_message_id=None,
                    quote_message_id=None,
                    links=[],
                    attachments=[],
                    is_system=False,
                ),
            ]

    class TimeOrderAnalyzer(FakeAnalyzer):
        def analyze_batch(self, target_date, batch_input):
            return BatchAnalysisResult(
                candidate_events=[
                    _draft(
                        draft_id="draft-late",
                        topic="较晚事件",
                        content="11点发生",
                        source_message_ids=["om_late"],
                        source_slice_id=batch_input.slices[0].slice_id,
                        object_hint="较晚事件结果",
                        retention_detail="确认较晚事件在11点形成结果。",
                    ),
                    _draft(
                        draft_id="draft-early",
                        topic="较早事件",
                        content="9点发生",
                        source_message_ids=["om_early"],
                        source_slice_id=batch_input.slices[0].slice_id,
                        object_hint="较早事件结果",
                        retention_detail="确认较早事件在9点形成结果。",
                    ),
                ],
                context_requests=[],
            )

        def merge_day_candidates(self, target_date, candidates, *, validation_feedback=""):
            return CrossConversationGroupResult(
                groups=[
                        CrossConversationGroup(
                            group_id="g1",
                            draft_ids=["draft-late"],
                            primary_draft_id="draft-late",
                        ),
                        CrossConversationGroup(
                            group_id="g2",
                            draft_ids=["draft-early"],
                            primary_draft_id="draft-early",
                        ),
                ]
            )

    config = RuntimeConfig(data_root=tmp_path / "data")
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=OutOfOrderSource(),
            content_resolver=FakeResolver(),
            analyzer=TimeOrderAnalyzer(),
            delivery_channel=FakeDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert result.output_path is not None

    content = Path(result.output_path).read_text(encoding="utf-8")
    assert content.index("### 1. 较早事件") < content.index("### 2. 较晚事件")


def test_runner_passes_self_identity_into_batch_input(tmp_path: Path) -> None:
    captured_batches = []

    class CapturingAnalyzer(FakeAnalyzer):
        def analyze_batch(self, target_date, batch_input):
            captured_batches.append(batch_input)
            return super().analyze_batch(target_date, batch_input)

    config = RuntimeConfig(data_root=tmp_path / "data")
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=FakeSource(),
            content_resolver=FakeResolver(),
            analyzer=CapturingAnalyzer(),
            delivery_channel=FakeDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert len(captured_batches) == 1
    assert captured_batches[0].self_open_id == "ou_self"
    assert captured_batches[0].self_display_name == "Me"
