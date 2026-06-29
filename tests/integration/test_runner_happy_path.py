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


class FakeAnalyzer:
    def build_batch_prompt(self, batch_input):
        return "batch prompt"

    def analyze_batch(self, target_date, batch_input):
        return BatchAnalysisResult(
            candidate_events=[
                SourceBackedEventDraft(
                    draft_id="draft-1",
                    date="2026-06-22",
                    topic="发布推进",
                    content="完成发布沟通",
                    source_message_ids=["om_1"],
                    source_conversation_id="oc_1",
                    source_slice_id=batch_input.slices[0].slice_id,
                    confidence=0.9,
                )
            ],
            context_requests=[],
        )

    def merge_day_candidates(self, target_date, candidates):
        raise AssertionError("Should not group when there is only one candidate")


class FakeDelivery:
    def deliver_to_self(self, *, self_identity, markdown_path):
        return ("success", self_identity.open_id)


def test_runner_happy_path(tmp_path: Path) -> None:
    config = RuntimeConfig(data_root=tmp_path / "data")
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
    assert result.self_delivery_status == "success"
    assert result.self_delivery_target == "ou_self"
    assert not (tmp_path / "data" / "debug" / "conversations").exists()


def test_runner_dumps_first_pass_conversation_debug_artifacts(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        conversation_debug_root=tmp_path / "debug",
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
    pass_dir = tmp_path / "debug" / "2026-06-22" / "oc_1__om_1" / "pass_01"
    assert (pass_dir / "input.json").exists()
    assert (pass_dir / "prompt.txt").read_text(encoding="utf-8") == "batch prompt"
    assert (pass_dir / "output.json").exists()
    meta = (pass_dir / "meta.json").read_text(encoding="utf-8")
    assert '"status": "completed"' in meta
    assert '"candidate_event_count": 1' in meta


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
                    SourceBackedEventDraft(
                        draft_id="draft-1",
                        date="2026-06-22",
                        topic="发布推进",
                        content="完成发布沟通",
                        source_message_ids=["om_1", "om_3"],
                        source_conversation_id="oc_1",
                        source_slice_id=batch_input.slices[0].slice_id,
                        confidence=0.9,
                    )
                ],
                context_requests=[],
            )

        def merge_day_candidates(self, target_date, candidates):
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
                    SourceBackedEventDraft(
                        draft_id="draft-1",
                        date="2026-06-22",
                        topic="索取全国故障汇总",
                        content="要求获取本周全国故障汇总。",
                        source_message_ids=["om_1"],
                        source_conversation_id="oc_1",
                        source_slice_id=batch_input.slices[0].slice_id,
                        confidence=0.9,
                    ),
                    SourceBackedEventDraft(
                        draft_id="draft-2",
                        date="2026-06-22",
                        topic="权限重置确认",
                        content="郭海重置了被封的权限/账号，并确认已知晓。",
                        source_message_ids=["om_1"],
                        source_conversation_id="oc_1",
                        source_slice_id=batch_input.slices[0].slice_id,
                        confidence=0.9,
                    ),
                ],
                context_requests=[],
            )

        def merge_day_candidates(self, target_date, candidates):
            return CrossConversationGroupResult(
                groups=[
                    CrossConversationGroup(group_id="g1", draft_ids=["draft-1"]),
                    CrossConversationGroup(group_id="g2", draft_ids=["draft-2"]),
                ]
            )

    config = RuntimeConfig(data_root=tmp_path / "data")
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
    assert "### 索取全国故障汇总" in content
    assert "### 权限重置确认" in content
    assert "要求获取本周全国故障汇总。" in content
    assert "郭海重置了被封的权限/账号，并确认已知晓。" in content


def test_runner_excludes_configured_topics_before_merge(tmp_path: Path) -> None:
    class ExcludedTopicAnalyzer(FakeAnalyzer):
        def analyze_batch(self, target_date, batch_input):
            return BatchAnalysisResult(
                candidate_events=[
                    SourceBackedEventDraft(
                        draft_id="draft-1",
                        date="2026-06-22",
                        topic="代码同步",
                        content="执行 git pull 操作，可能涉及代码更新同步。",
                        source_message_ids=["om_1"],
                        source_conversation_id="oc_1",
                        source_slice_id=batch_input.slices[0].slice_id,
                        confidence=0.9,
                    ),
                    SourceBackedEventDraft(
                        draft_id="draft-2",
                        date="2026-06-22",
                        topic="权限重置确认",
                        content="郭海重置了被封的权限/账号，并确认已知晓。",
                        source_message_ids=["om_1"],
                        source_conversation_id="oc_1",
                        source_slice_id=batch_input.slices[0].slice_id,
                        confidence=0.9,
                    ),
                ],
                context_requests=[],
            )

        def merge_day_candidates(self, target_date, candidates):
            raise AssertionError("Excluded topics should be filtered before merge")

    config = RuntimeConfig(data_root=tmp_path / "data")
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
    assert "### 权限重置确认" in content
    assert "### 代码同步" not in content
    assert "执行 git pull 操作" not in content


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
                    SourceBackedEventDraft(
                        draft_id="draft-late",
                        date="2026-06-22",
                        topic="较晚事件",
                        content="11点发生",
                        source_message_ids=["om_late"],
                        source_conversation_id="oc_1",
                        source_slice_id=batch_input.slices[0].slice_id,
                        confidence=0.9,
                    ),
                    SourceBackedEventDraft(
                        draft_id="draft-early",
                        date="2026-06-22",
                        topic="较早事件",
                        content="9点发生",
                        source_message_ids=["om_early"],
                        source_conversation_id="oc_1",
                        source_slice_id=batch_input.slices[0].slice_id,
                        confidence=0.9,
                    ),
                ],
                context_requests=[],
            )

        def merge_day_candidates(self, target_date, candidates):
            return CrossConversationGroupResult(
                groups=[
                    CrossConversationGroup(group_id="g1", draft_ids=["draft-late"]),
                    CrossConversationGroup(group_id="g2", draft_ids=["draft-early"]),
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
    assert content.index("### 较早事件") < content.index("### 较晚事件")


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
