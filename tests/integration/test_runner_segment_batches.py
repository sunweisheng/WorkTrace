from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from src.worktrace.config import RuntimeConfig
from src.worktrace.constants import DailyRunStatus
from src.worktrace.errors import AnalyzerProtocolError
from src.worktrace.factories import RuntimeDependencies
from src.worktrace.models import (
    AnchorAnalysisResult,
    AnchorUnit,
    AttachmentMeta,
    AttachmentTextBlock,
    BatchAnalysisResult,
    BatchAnchorAnalysisItem,
    BatchAnchorAnalysisResult,
    BatchSegmentAnalysisItem,
    BatchSegmentAnalysisResult,
    ConversationRef,
    ConversationSegment,
    ConversationSegmentationResult,
    ContextRequest,
    CrossConversationGroup,
    CrossConversationGroupResult,
    NormalizedMessage,
    LinkMeta,
    SelfIdentity,
    SourceBackedEventDraft,
)
from src.worktrace.runner import DailyTraceRunner
from src.worktrace.stores.markdown import MarkdownEventStore


def _message(
    message_id: str,
    *,
    sender_open_id: str,
    minute: int,
    text: str,
    mentioned_open_ids: list[str] | None = None,
) -> NormalizedMessage:
    return NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id=message_id,
        sender_open_id=sender_open_id,
        sender_name=sender_open_id,
        send_time=f"2026-07-10T09:{minute:02d}:00+08:00",
        message_type="text",
        text=text,
        reply_to_message_id=None,
        quote_message_id=None,
        mentioned_open_ids=mentioned_open_ids or [],
    )


def _candidate(draft_id: str, source_message_id: str) -> SourceBackedEventDraft:
    return SourceBackedEventDraft(
        draft_id=draft_id,
        date="2026-07-10",
        topic=f"事项{draft_id}",
        content=f"确认事项{draft_id}的具体结论并继续推进。",
        source_message_ids=[source_message_id],
        source_conversation_id="",
        source_slice_id="",
        confidence=0.9,
        action_label="确认",
        object_hint=f"事项{draft_id}对象",
        retention_reason="decision_made",
        retention_detail=f"沟通中确认了事项{draft_id}的具体结论。",
    )


def _anchor_unit(index: int) -> AnchorUnit:
    message = _message(
        f"om_anchor_{index}",
        sender_open_id="ou_self",
        minute=index,
        text=f"锚点事项{index}",
    )
    return AnchorUnit(
        anchor_unit_id=f"anchor-{index}",
        conversation_id="oc_1",
        conversation_name="项目群",
        anchor_message_ids=[message.message_id],
        in_day_message_ids=[message.message_id],
        base_message_ids=[message.message_id],
        messages=[message],
    )


class SegmentSource:
    def get_self_identity(self):
        return SelfIdentity(open_id="ou_self", display_name="张宝华", source="test")

    def list_target_conversations(self, target_date, self_identity):
        return [ConversationRef(conversation_id="oc_1", conversation_name="项目群")]

    def fetch_conversation_messages(self, target_date, conversation_ids):
        return [
            _message("om_1", sender_open_id="ou_self", minute=0, text="我会推进发布方案"),
            _message("om_2", sender_open_id="ou_ding", minute=1, text="请同步发布结论"),
            _message("om_3", sender_open_id="ou_self", minute=2, text="我会核对风险清单"),
            _message(
                "om_4",
                sender_open_id="ou_ding",
                minute=3,
                text="请张玉环安排会议",
                mentioned_open_ids=["ou_yuhuan"],
            ),
            _message("om_5", sender_open_id="ou_yuhuan", minute=4, text="我来处理会议"),
        ]

    def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
        return []


class SegmentResolver:
    def to_text(self, message):
        return message.text

    def extract_links(self, message):
        return list(message.links)

    def load_attachment_text_if_needed(self, message, attachment_ids, hint):
        return []

    def load_link_text_if_needed(self, message, link_ids, hint):
        return []


class SegmentDelivery:
    def deliver_to_self(self, *, self_identity, markdown_path):
        return ("success", self_identity.open_id)


class SegmentBatchAnalyzer:
    def __init__(self) -> None:
        self.segmentation_calls = 0
        self.batch_calls = 0
        self.batch_segment_ids: list[list[str]] = []

    def segment_conversation(self, **kwargs):
        self.segmentation_calls += 1
        return ConversationSegmentationResult(
            segments=[
                ConversationSegment(
                    segment_id="turn-release",
                    primary_message_ids=["om_1", "om_2"],
                    self_evidence_message_ids=["om_1"],
                ),
                ConversationSegment(
                    segment_id="turn-risk",
                    primary_message_ids=["om_3"],
                    self_evidence_message_ids=["om_3"],
                ),
                ConversationSegment(
                    segment_id="turn-yuhuan",
                    primary_message_ids=["om_4", "om_5"],
                ),
            ]
        )

    def analyze_segment_batch(self, batch):
        self.batch_calls += 1
        self.batch_segment_ids.append([unit.segment_id for unit in batch.segments])
        source_ids = {
            "turn-release": "om_1",
            "turn-risk": "om_3",
        }
        return BatchSegmentAnalysisResult(
            results=[
                BatchSegmentAnalysisItem(
                    unit.segment_id,
                    BatchAnalysisResult(
                        candidate_events=[
                            _candidate(
                                unit.segment_id,
                                source_ids[unit.segment_id.rsplit(":", 1)[-1]],
                            )
                        ]
                    ),
                )
                for unit in batch.segments
            ]
        )

    def merge_day_candidates(self, target_date, candidates):
        return CrossConversationGroupResult(
            groups=[
                CrossConversationGroup(group_id=item.draft_id, draft_ids=[item.draft_id])
                for item in candidates
            ]
        )


def test_runner_batches_multiple_self_turns_and_excludes_other_recipient_turn(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(data_root=tmp_path / "data")
    analyzer = SegmentBatchAnalyzer()
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=SegmentSource(),
            content_resolver=SegmentResolver(),
            analyzer=analyzer,
            delivery_channel=SegmentDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-07-10")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert result.event_count == 2
    assert analyzer.segmentation_calls == 1
    assert analyzer.batch_calls == 1
    assert analyzer.batch_segment_ids == [[
        "anchor-001:turn-release",
        "anchor-002:turn-risk",
    ]]
    assert result.batch_count == 2


def test_runner_retries_only_the_invalid_anchor_segmentation(tmp_path: Path) -> None:
    class RetryingAnalyzer(SegmentBatchAnalyzer):
        def segment_conversation(self, **kwargs):
            self.segmentation_calls += 1
            if self.segmentation_calls == 1:
                return ConversationSegmentationResult()
            return ConversationSegmentationResult(
                segments=[
                    ConversationSegment(
                        segment_id="turn-release",
                        primary_message_ids=["om_1", "om_2"],
                        self_evidence_message_ids=["om_1"],
                    ),
                    ConversationSegment(
                        segment_id="turn-risk",
                        primary_message_ids=["om_3"],
                        self_evidence_message_ids=["om_3"],
                    ),
                    ConversationSegment(
                        segment_id="turn-yuhuan",
                        primary_message_ids=["om_4", "om_5"],
                    ),
                ]
            )

    config = RuntimeConfig(data_root=tmp_path / "data", anchor_retry_limit=1)
    analyzer = RetryingAnalyzer()
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=SegmentSource(),
            content_resolver=SegmentResolver(),
            analyzer=analyzer,
            delivery_channel=SegmentDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-07-10")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert result.event_count == 2
    assert analyzer.segmentation_calls == 2
    assert analyzer.batch_calls == 1


def test_runner_segments_each_original_anchor_window_not_the_full_conversation(
    tmp_path: Path,
) -> None:
    class LongConversationSource(SegmentSource):
        def fetch_conversation_messages(self, target_date, conversation_ids):
            messages = []
            for index in range(80):
                messages.append(
                    NormalizedMessage(
                        conversation_id="oc_1",
                        conversation_name="项目群",
                        message_id=f"om_{index:03d}",
                        sender_open_id=("ou_self" if index == 40 else "ou_other"),
                        sender_name="张宝华" if index == 40 else "同事",
                        send_time=(
                            f"2026-07-10T09:{index // 60:02d}:{index % 60:02d}+08:00"
                        ),
                        message_type="text",
                        text=f"第 {index} 条沟通",
                        reply_to_message_id=None,
                        quote_message_id=None,
                    )
                )
            return messages

    class WindowAnalyzer:
        def __init__(self) -> None:
            self.segmentation_inputs: list[list[str]] = []

        def segment_conversation(self, **kwargs):
            message_ids = [item.message_id for item in kwargs["messages"]]
            self.segmentation_inputs.append(message_ids)
            return ConversationSegmentationResult(
                segments=[
                    ConversationSegment(
                        segment_id="turn-window",
                        primary_message_ids=message_ids,
                        self_evidence_message_ids=["om_040"],
                    )
                ]
            )

        def analyze_segment_batch(self, batch):
            return BatchSegmentAnalysisResult(
                results=[
                    BatchSegmentAnalysisItem(
                        unit.segment_id,
                        BatchAnalysisResult(),
                    )
                    for unit in batch.segments
                ]
            )

    analyzer = WindowAnalyzer()
    config = RuntimeConfig(data_root=tmp_path / "data")
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=LongConversationSource(),
            content_resolver=SegmentResolver(),
            analyzer=analyzer,
            delivery_channel=SegmentDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-07-10")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert analyzer.segmentation_inputs == [
        [f"om_{index:03d}" for index in range(10, 71)]
    ]


def test_runner_resegments_only_the_context_requesting_turn(tmp_path: Path) -> None:
    class ExpansionSource(SegmentSource):
        def fetch_conversation_messages(self, target_date, conversation_ids):
            message = _message(
                "om_1",
                sender_open_id="ou_self",
                minute=0,
                text="我会核对发布附件",
            )
            return [
                NormalizedMessage(
                    **(message.__dict__ | {
                        "attachments": [
                            AttachmentMeta(
                                attachment_id="file-1",
                                file_name="发布清单.txt",
                                mime_type="text/plain",
                                file_size=None,
                            )
                        ]
                    })
                ),
                _message(
                    "om_2",
                    sender_open_id="ou_other",
                    minute=1,
                    text="请确认附件中的发布范围",
                ),
            ]

    class ExpansionResolver(SegmentResolver):
        def load_attachment_text_if_needed(self, message, attachment_ids, hint):
            return [
                AttachmentTextBlock(
                    attachment_id="file-1",
                    message_id=message.message_id,
                    file_name="发布清单.txt",
                    text="发布范围已确认。",
                )
            ]

    class ExpansionAnalyzer:
        def __init__(self) -> None:
            self.segmentation_calls = 0
            self.batch_calls = 0

        def segment_conversation(self, **kwargs):
            self.segmentation_calls += 1
            return ConversationSegmentationResult(
                segments=[
                    ConversationSegment(
                        segment_id="turn-attachment",
                        primary_message_ids=["om_1", "om_2"],
                        self_evidence_message_ids=["om_1"],
                    )
                ]
            )

        def analyze_segment_batch(self, batch):
            self.batch_calls += 1
            if self.batch_calls == 1:
                return BatchSegmentAnalysisResult(
                    results=[
                        BatchSegmentAnalysisItem(
                            batch.segments[0].segment_id,
                            BatchAnalysisResult(
                                context_requests=[
                                    ContextRequest(
                                        slice_id="",
                                        request_type="attachment_text",
                                        target_message_ids=["om_1"],
                                        target_attachment_ids=["file-1"],
                                        reason="需要核对附件",
                                    )
                                ]
                            ),
                        )
                    ]
                )
            return BatchSegmentAnalysisResult(
                results=[
                    BatchSegmentAnalysisItem(
                        batch.segments[0].segment_id,
                        BatchAnalysisResult(
                            candidate_events=[_candidate("attachment", "om_1")]
                        ),
                    )
                ]
            )

        def merge_day_candidates(self, target_date, candidates):
            raise AssertionError("One candidate should not need day merge.")

    config = RuntimeConfig(data_root=tmp_path / "data")
    analyzer = ExpansionAnalyzer()
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=ExpansionSource(),
            content_resolver=ExpansionResolver(),
            analyzer=analyzer,
            delivery_channel=SegmentDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-07-10")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert result.event_count == 1
    assert analyzer.segmentation_calls == 2
    assert analyzer.batch_calls == 2
    assert result.batch_count == 4


def test_runner_splits_segment_batches_in_timeline_order_when_token_limit_is_hit(
    tmp_path: Path,
) -> None:
    class SplitAnalyzer(SegmentBatchAnalyzer):
        def analyze_segment_batch(self, batch):
            self.batch_calls += 1
            self.batch_segment_ids.append([unit.segment_id for unit in batch.segments])
            source_ids = {
                unit.segment_id: unit.primary_message_ids[0]
                for unit in batch.segments
            }
            return BatchSegmentAnalysisResult(
                results=[
                    BatchSegmentAnalysisItem(
                        unit.segment_id,
                        BatchAnalysisResult(
                            candidate_events=[
                                _candidate(unit.segment_id, source_ids[unit.segment_id])
                            ]
                        ),
                    )
                    for unit in batch.segments
                ]
            )

    config = RuntimeConfig(data_root=tmp_path / "data", max_model_input_tokens=1)
    analyzer = SplitAnalyzer()
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=SegmentSource(),
            content_resolver=SegmentResolver(),
            analyzer=analyzer,
            delivery_channel=SegmentDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-07-10")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert analyzer.batch_segment_ids == [
        ["anchor-001:turn-release"],
        ["anchor-002:turn-risk"],
    ]
    assert analyzer.batch_calls == 2


def test_runner_retries_failed_batch_then_degrades_to_individual_segments(
    tmp_path: Path,
) -> None:
    class FallbackAnalyzer(SegmentBatchAnalyzer):
        def analyze_segment_batch(self, batch):
            self.batch_calls += 1
            self.batch_segment_ids.append([unit.segment_id for unit in batch.segments])
            if self.batch_calls <= 2 or batch.segments[0].segment_id.endswith("turn-risk"):
                raise AnalyzerProtocolError("temporary model failure")
            return BatchSegmentAnalysisResult(
                results=[
                    BatchSegmentAnalysisItem(
                        batch.segments[0].segment_id,
                        BatchAnalysisResult(candidate_events=[_candidate("release", "om_1")]),
                    )
                ]
            )

    config = RuntimeConfig(data_root=tmp_path / "data")
    analyzer = FallbackAnalyzer()
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=SegmentSource(),
            content_resolver=SegmentResolver(),
            analyzer=analyzer,
            delivery_channel=SegmentDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-07-10")

    assert result.status == DailyRunStatus.SUCCESS_WITH_WARNINGS.value
    assert result.event_count == 1
    assert analyzer.batch_segment_ids == [
        ["anchor-001:turn-release", "anchor-002:turn-risk"],
        ["anchor-001:turn-release", "anchor-002:turn-risk"],
        ["anchor-001:turn-release"],
        ["anchor-002:turn-risk"],
    ]
    assert result.skipped_slice_count == 1


def test_runner_falls_back_to_all_conversation_anchors_after_segmentation_exhaustion(
    tmp_path: Path,
) -> None:
    class ConversationFallbackAnalyzer(SegmentBatchAnalyzer):
        def __init__(self) -> None:
            super().__init__()
            self.anchor_batches: list[list[str]] = []

        def segment_conversation(self, **kwargs):
            self.segmentation_calls += 1
            if self.segmentation_calls == 1:
                return ConversationSegmentationResult()
            return super().segment_conversation(**kwargs)

        def analyze_anchor_batch(self, target_date, anchor_units):
            self.anchor_batches.append([item.anchor_unit_id for item in anchor_units])
            return BatchAnchorAnalysisResult(
                results=[
                    BatchAnchorAnalysisItem(
                        anchor_unit_id=unit.anchor_unit_id,
                        analysis=AnchorAnalysisResult(
                            anchor_status="completed",
                            candidate_events=[
                                _candidate(
                                    f"fallback-{index}",
                                    unit.anchor_message_ids[0],
                                )
                            ],
                        ),
                    )
                    for index, unit in enumerate(anchor_units, start=1)
                ]
            )

    config = RuntimeConfig(data_root=tmp_path / "data", anchor_retry_limit=0)
    analyzer = ConversationFallbackAnalyzer()
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=SegmentSource(),
            content_resolver=SegmentResolver(),
            analyzer=analyzer,
            delivery_channel=SegmentDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-07-10")

    assert result.status == DailyRunStatus.SUCCESS_WITH_WARNINGS.value
    assert analyzer.segmentation_calls == 1
    assert analyzer.batch_calls == 0
    assert len(analyzer.anchor_batches) == 1
    assert len(analyzer.anchor_batches[0]) == 2


def test_anchor_fallback_retries_the_same_batch_before_splitting(tmp_path: Path) -> None:
    class RetryAnalyzer:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def analyze_anchor_batch(self, target_date, anchor_units):
            self.calls.append([item.anchor_unit_id for item in anchor_units])
            if len(self.calls) == 1:
                raise AnalyzerProtocolError("temporary failure")
            return BatchAnchorAnalysisResult(
                results=[
                    BatchAnchorAnalysisItem(
                        anchor_unit_id=item.anchor_unit_id,
                        analysis=AnchorAnalysisResult(anchor_status="completed"),
                    )
                    for item in anchor_units
                ]
            )

    analyzer = RetryAnalyzer()
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        anchor_batch_size=4,
        anchor_batch_retry_limit=1,
    )
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=SegmentSource(),
            content_resolver=SegmentResolver(),
            analyzer=analyzer,
            delivery_channel=SegmentDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    results, warnings, skipped_count, call_count = runner._analyze_anchor_units_resilient(
        target_date="2026-07-10",
        anchor_units=[_anchor_unit(index) for index in range(1, 5)],
    )

    assert list(results) == ["anchor-1", "anchor-2", "anchor-3", "anchor-4"]
    assert warnings == []
    assert skipped_count == 0
    assert call_count == 2
    assert analyzer.calls == [
        ["anchor-1", "anchor-2", "anchor-3", "anchor-4"],
        ["anchor-1", "anchor-2", "anchor-3", "anchor-4"],
    ]


def test_anchor_fallback_bisects_only_persistently_failing_batches(tmp_path: Path) -> None:
    class SplittingAnalyzer:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def analyze_anchor_batch(self, target_date, anchor_units):
            ids = [item.anchor_unit_id for item in anchor_units]
            self.calls.append(ids)
            if "anchor-4" in ids:
                raise AnalyzerProtocolError("anchor-4 cannot be analyzed")
            return BatchAnchorAnalysisResult(
                results=[
                    BatchAnchorAnalysisItem(
                        anchor_unit_id=item.anchor_unit_id,
                        analysis=AnchorAnalysisResult(anchor_status="completed"),
                    )
                    for item in anchor_units
                ]
            )

    analyzer = SplittingAnalyzer()
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        anchor_batch_size=4,
        anchor_batch_retry_limit=1,
    )
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=SegmentSource(),
            content_resolver=SegmentResolver(),
            analyzer=analyzer,
            delivery_channel=SegmentDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    results, _, skipped_count, _ = runner._analyze_anchor_units_resilient(
        target_date="2026-07-10",
        anchor_units=[_anchor_unit(index) for index in range(1, 5)],
    )

    assert set(results) == {"anchor-1", "anchor-2", "anchor-3"}
    assert skipped_count == 1
    assert analyzer.calls[:2] == [
        ["anchor-1", "anchor-2", "anchor-3", "anchor-4"],
        ["anchor-1", "anchor-2", "anchor-3", "anchor-4"],
    ]
    assert ["anchor-1", "anchor-2"] in analyzer.calls
    assert ["anchor-3", "anchor-4"] in analyzer.calls
    assert analyzer.calls.count(["anchor-4"]) == 2
    assert ["anchor-1"] not in analyzer.calls
    assert ["anchor-2"] not in analyzer.calls


def test_anchor_fallback_rebatches_only_missing_results(tmp_path: Path) -> None:
    class PartialAnalyzer:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def analyze_anchor_batch(self, target_date, anchor_units):
            ids = [item.anchor_unit_id for item in anchor_units]
            self.calls.append(ids)
            selected = anchor_units[:1] if len(ids) == 4 else anchor_units
            return BatchAnchorAnalysisResult(
                results=[
                    BatchAnchorAnalysisItem(
                        anchor_unit_id=item.anchor_unit_id,
                        analysis=AnchorAnalysisResult(anchor_status="completed"),
                    )
                    for item in selected
                ]
            )

    analyzer = PartialAnalyzer()
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        anchor_batch_size=4,
        anchor_batch_retry_limit=1,
    )
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=SegmentSource(),
            content_resolver=SegmentResolver(),
            analyzer=analyzer,
            delivery_channel=SegmentDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    results, _, skipped_count, _ = runner._analyze_anchor_units_resilient(
        target_date="2026-07-10",
        anchor_units=[_anchor_unit(index) for index in range(1, 5)],
    )

    assert len(results) == 4
    assert skipped_count == 0
    assert analyzer.calls == [
        ["anchor-1", "anchor-2", "anchor-3", "anchor-4"],
        ["anchor-1", "anchor-2", "anchor-3", "anchor-4"],
        ["anchor-2", "anchor-3", "anchor-4"],
    ]


def test_anchor_fallback_rebatches_same_type_expansions(tmp_path: Path) -> None:
    class AttachmentResolver(SegmentResolver):
        def load_attachment_text_if_needed(self, message, attachment_ids, hint):
            return [
                AttachmentTextBlock(
                    attachment_id=attachment_ids[0],
                    message_id=message.message_id,
                    file_name="上下文.txt",
                    text="补充上下文",
                )
            ]

    class ExpansionAnalyzer:
        def __init__(self) -> None:
            self.calls: list[tuple[list[str], list[bool]]] = []

        def analyze_anchor_batch(self, target_date, anchor_units):
            self.calls.append(
                (
                    [item.anchor_unit_id for item in anchor_units],
                    [bool(item.attachment_texts) for item in anchor_units],
                )
            )
            analyses = []
            for unit in anchor_units:
                analysis = (
                    AnchorAnalysisResult(anchor_status="completed")
                    if unit.attachment_texts
                    else AnchorAnalysisResult(
                        anchor_status="needs_attachment_text",
                        context_requests=[
                            ContextRequest(
                                slice_id="",
                                request_type="attachment_text",
                                target_message_ids=[unit.messages[0].message_id],
                                target_attachment_ids=[unit.attachment_refs[0].attachment_id],
                            )
                        ],
                    )
                )
                analyses.append(BatchAnchorAnalysisItem(unit.anchor_unit_id, analysis))
            return BatchAnchorAnalysisResult(results=analyses)

    units = []
    for index in (1, 2):
        unit = _anchor_unit(index)
        attachment = AttachmentMeta(
            attachment_id=f"file-{index}",
            file_name=f"上下文-{index}.txt",
            mime_type="text/plain",
            file_size=None,
        )
        units.append(
            replace(
                unit,
                messages=[replace(unit.messages[0], attachments=[attachment])],
                attachment_refs=[attachment],
            )
        )

    analyzer = ExpansionAnalyzer()
    config = RuntimeConfig(data_root=tmp_path / "data", anchor_batch_size=2)
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=SegmentSource(),
            content_resolver=AttachmentResolver(),
            analyzer=analyzer,
            delivery_channel=SegmentDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    candidates, warnings, skipped_count, call_count = runner._analyze_anchor_fallback(
        target_date="2026-07-10",
        anchor_units=units,
        self_identity=SelfIdentity("ou_self", "张宝华", "test"),
    )

    assert candidates == []
    assert warnings == []
    assert skipped_count == 0
    assert call_count == 2
    assert analyzer.calls == [
        (["anchor-1", "anchor-2"], [False, False]),
        (["anchor-1", "anchor-2"], [True, True]),
    ]


def test_anchor_fallback_separates_different_expansion_types(tmp_path: Path) -> None:
    class MixedSource(SegmentSource):
        def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
            return [
                _message(
                    "om_related",
                    sender_open_id="ou_other",
                    minute=50,
                    text="补充前后消息",
                )
            ]

    class MixedResolver(SegmentResolver):
        def load_attachment_text_if_needed(self, message, attachment_ids, hint):
            return [
                AttachmentTextBlock(
                    attachment_id=attachment_ids[0],
                    message_id=message.message_id,
                    file_name="上下文.txt",
                    text="附件上下文",
                )
            ]

    class MixedAnalyzer:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def analyze_anchor_batch(self, target_date, anchor_units):
            self.calls.append([item.anchor_unit_id for item in anchor_units])
            results = []
            for unit in anchor_units:
                if unit.anchor_unit_id == "anchor-1" and not unit.attachment_texts:
                    analysis = AnchorAnalysisResult(
                        anchor_status="needs_attachment_text",
                        context_requests=[
                            ContextRequest(
                                slice_id="",
                                request_type="attachment_text",
                                target_message_ids=[unit.messages[0].message_id],
                                target_attachment_ids=[unit.attachment_refs[0].attachment_id],
                            )
                        ],
                    )
                elif unit.anchor_unit_id == "anchor-2" and len(unit.messages) == 1:
                    analysis = AnchorAnalysisResult(
                        anchor_status="needs_more_context",
                        context_requests=[
                            ContextRequest(
                                slice_id="",
                                request_type="later_messages",
                                target_message_ids=[unit.messages[0].message_id],
                            )
                        ],
                    )
                else:
                    analysis = AnchorAnalysisResult(anchor_status="completed")
                results.append(BatchAnchorAnalysisItem(unit.anchor_unit_id, analysis))
            return BatchAnchorAnalysisResult(results=results)

    first = _anchor_unit(1)
    attachment = AttachmentMeta("file-1", "上下文.txt", "text/plain", None)
    first = replace(
        first,
        messages=[replace(first.messages[0], attachments=[attachment])],
        attachment_refs=[attachment],
    )
    analyzer = MixedAnalyzer()
    config = RuntimeConfig(data_root=tmp_path / "data", anchor_batch_size=2)
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=MixedSource(),
            content_resolver=MixedResolver(),
            analyzer=analyzer,
            delivery_channel=SegmentDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    _, warnings, skipped_count, call_count = runner._analyze_anchor_fallback(
        target_date="2026-07-10",
        anchor_units=[first, _anchor_unit(2)],
        self_identity=SelfIdentity("ou_self", "张宝华", "test"),
    )

    assert warnings == []
    assert skipped_count == 0
    assert call_count == 3
    assert analyzer.calls == [
        ["anchor-1", "anchor-2"],
        ["anchor-1"],
        ["anchor-2"],
    ]


def test_runner_stops_remaining_segmentation_after_same_failure_threshold(
    tmp_path: Path,
) -> None:
    class LongConversationSource(SegmentSource):
        def fetch_conversation_messages(self, target_date, conversation_ids):
            return [
                NormalizedMessage(
                    conversation_id="oc_1",
                    conversation_name="项目群",
                    message_id=f"om_{index:03d}",
                    sender_open_id="ou_self" if index in {5, 37, 70} else "ou_other",
                    sender_name="张宝华" if index in {5, 37, 70} else "同事",
                    send_time=f"2026-07-10T09:{index // 60:02d}:{index % 60:02d}+08:00",
                    message_type="text",
                    text=f"消息 {index}",
                    reply_to_message_id=None,
                    quote_message_id=None,
                )
                for index in range(81)
            ]

    class CircuitAnalyzer(SegmentBatchAnalyzer):
        def __init__(self) -> None:
            super().__init__()
            self.segmentation_inputs: list[list[str]] = []
            self.anchor_batches: list[list[str]] = []

        def segment_conversation(self, **kwargs):
            self.segmentation_inputs.append(
                [item.message_id for item in kwargs["messages"]]
            )
            return ConversationSegmentationResult()

        def analyze_anchor_batch(self, target_date, anchor_units):
            self.anchor_batches.append([item.anchor_unit_id for item in anchor_units])
            return BatchAnchorAnalysisResult(
                results=[
                    BatchAnchorAnalysisItem(
                        anchor_unit_id=item.anchor_unit_id,
                        analysis=AnchorAnalysisResult(anchor_status="completed"),
                    )
                    for item in anchor_units
                ]
            )

    analyzer = CircuitAnalyzer()
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        anchor_retry_limit=0,
        conversation_segmentation_failure_threshold=2,
        anchor_batch_size=3,
    )
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=LongConversationSource(),
            content_resolver=SegmentResolver(),
            analyzer=analyzer,
            delivery_channel=SegmentDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-07-10")

    assert result.status == DailyRunStatus.SUCCESS_WITH_WARNINGS.value
    assert len(analyzer.segmentation_inputs) == 2
    assert len(analyzer.anchor_batches) == 1
    assert len(analyzer.anchor_batches[0]) == 3
    assert "Stopped remaining anchor segmentation after repeated" in result.error_summary


def test_runner_keeps_protocol_and_validation_segmentation_failures_separate(
    tmp_path: Path,
) -> None:
    class LongConversationSource(SegmentSource):
        def fetch_conversation_messages(self, target_date, conversation_ids):
            return [
                NormalizedMessage(
                    conversation_id="oc_1",
                    conversation_name="项目群",
                    message_id=f"om_{index:03d}",
                    sender_open_id="ou_self" if index in {5, 37, 70} else "ou_other",
                    sender_name="张宝华" if index in {5, 37, 70} else "同事",
                    send_time=f"2026-07-10T09:{index // 60:02d}:{index % 60:02d}+08:00",
                    message_type="text",
                    text=f"消息 {index}",
                    reply_to_message_id=None,
                    quote_message_id=None,
                )
                for index in range(81)
            ]

    class MixedFailureAnalyzer(SegmentBatchAnalyzer):
        def __init__(self) -> None:
            super().__init__()
            self.segment_calls = 0

        def segment_conversation(self, **kwargs):
            self.segment_calls += 1
            if self.segment_calls == 1:
                return ConversationSegmentationResult()
            if self.segment_calls == 2:
                raise AnalyzerProtocolError("transient protocol failure")
            message_ids = [item.message_id for item in kwargs["messages"]]
            return ConversationSegmentationResult(
                segments=[ConversationSegment("turn", message_ids)]
            )

        def analyze_anchor_batch(self, target_date, anchor_units):
            return BatchAnchorAnalysisResult(
                results=[
                    BatchAnchorAnalysisItem(
                        anchor_unit_id=item.anchor_unit_id,
                        analysis=AnchorAnalysisResult(anchor_status="completed"),
                    )
                    for item in anchor_units
                ]
            )

        def analyze_segment_batch(self, batch):
            return BatchSegmentAnalysisResult(
                results=[
                    BatchSegmentAnalysisItem(unit.segment_id, BatchAnalysisResult())
                    for unit in batch.segments
                ]
            )

    analyzer = MixedFailureAnalyzer()
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        anchor_retry_limit=0,
        conversation_segmentation_failure_threshold=2,
    )
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=LongConversationSource(),
            content_resolver=SegmentResolver(),
            analyzer=analyzer,
            delivery_channel=SegmentDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    runner.run("2026-07-10")

    assert analyzer.segment_calls == 3


def test_runner_hydrates_document_title_before_anchor_segmentation(tmp_path: Path) -> None:
    class TitleSource(SegmentSource):
        def fetch_conversation_messages(self, target_date, conversation_ids):
            message = _message("om_1", sender_open_id="ou_self", minute=0, text="请确认文档")
            return [
                NormalizedMessage(
                    **(
                        message.__dict__
                        | {
                            "links": [
                                LinkMeta(
                                    url="https://example.feishu.cn/docx/doc-1",
                                    title="",
                                    link_type="feishu_doc",
                                )
                            ]
                        }
                    )
                )
            ]

    class TitleResolver(SegmentResolver):
        def extract_links(self, message):
            return [
                LinkMeta(
                    url="https://example.feishu.cn/docx/doc-1",
                    title="发布范围确认单",
                    link_type="feishu_doc",
                )
            ]

    class TitleAnalyzer:
        def __init__(self) -> None:
            self.titles: list[str] = []

        def segment_conversation(self, **kwargs):
            self.titles.append(kwargs["messages"][0].links[0].title)
            return ConversationSegmentationResult(
                segments=[ConversationSegment(segment_id="turn", primary_message_ids=["om_1"])]
            )

        def analyze_segment_batch(self, batch):
            return BatchSegmentAnalysisResult(
                results=[
                    BatchSegmentAnalysisItem(unit.segment_id, BatchAnalysisResult())
                    for unit in batch.segments
                ]
            )

    config = RuntimeConfig(data_root=tmp_path / "data")
    analyzer = TitleAnalyzer()
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=TitleSource(),
            content_resolver=TitleResolver(),
            analyzer=analyzer,
            delivery_channel=SegmentDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-07-10")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert analyzer.titles == ["发布范围确认单"]


def test_anchor_fallback_retries_only_context_requesting_anchor(tmp_path: Path) -> None:
    class AttachmentSource(SegmentSource):
        def fetch_conversation_messages(self, target_date, conversation_ids):
            message = _message("om_1", sender_open_id="ou_self", minute=0, text="请确认附件")
            return [
                NormalizedMessage(
                    **(
                        message.__dict__
                        | {
                            "attachments": [
                                AttachmentMeta(
                                    attachment_id="file-1",
                                    file_name="发布清单.txt",
                                    mime_type="text/plain",
                                    file_size=None,
                                )
                            ]
                        }
                    )
                )
            ]

    class AttachmentResolver(SegmentResolver):
        def load_attachment_text_if_needed(self, message, attachment_ids, hint):
            return [
                AttachmentTextBlock(
                    attachment_id="file-1",
                    message_id=message.message_id,
                    file_name="发布清单.txt",
                    text="发布范围已经确认。",
                )
            ]

    class AttachmentFallbackAnalyzer:
        def __init__(self) -> None:
            self.anchor_calls = 0
            self.expanded = False

        def segment_conversation(self, **kwargs):
            return ConversationSegmentationResult()

        def analyze_segment_batch(self, batch):
            return BatchSegmentAnalysisResult()

        def analyze_anchor_batch(self, target_date, anchor_units):
            self.anchor_calls += 1
            unit = anchor_units[0]
            self.expanded = self.expanded or bool(unit.attachment_texts)
            analysis = (
                AnchorAnalysisResult(
                    anchor_status="completed",
                    candidate_events=[_candidate("fallback", "om_1")],
                )
                if unit.attachment_texts
                else AnchorAnalysisResult(
                    anchor_status="needs_attachment_text",
                    context_requests=[
                        ContextRequest(
                            slice_id="",
                            request_type="attachment_text",
                            target_message_ids=["om_1"],
                            target_attachment_ids=["file-1"],
                            reason="需要读取附件",
                        )
                    ],
                )
            )
            return BatchAnchorAnalysisResult(
                results=[BatchAnchorAnalysisItem(unit.anchor_unit_id, analysis)]
            )

        def merge_day_candidates(self, target_date, candidates):
            raise AssertionError("One fallback candidate does not need day merge.")

    config = RuntimeConfig(data_root=tmp_path / "data", anchor_retry_limit=1)
    analyzer = AttachmentFallbackAnalyzer()
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=AttachmentSource(),
            content_resolver=AttachmentResolver(),
            analyzer=analyzer,
            delivery_channel=SegmentDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-07-10")

    assert result.status == DailyRunStatus.SUCCESS_WITH_WARNINGS.value
    assert result.event_count == 1
    assert analyzer.anchor_calls == 2
    assert analyzer.expanded is True
