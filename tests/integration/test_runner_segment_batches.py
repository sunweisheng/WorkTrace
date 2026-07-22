from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from threading import Lock
from time import sleep

from src.worktrace.config import EventMetadataItem, RuntimeConfig
from src.worktrace.constants import DailyRunStatus
from src.worktrace.errors import AnalyzerProtocolError, ModelInputRejectedError
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
    SelfRelationEvidence,
    SelfIdentity,
    SourceBackedEventDraft,
)
from src.worktrace.pipeline.conversation_segments import _estimate_segment_batch_tokens
from src.worktrace.reaction_catalog import ReactionCatalog
from src.worktrace.runner import (
    DailyTraceRunner,
    _estimate_anchor_batch_input_tokens,
    _estimate_day_merge_input_tokens,
    _estimate_segmentation_input_tokens,
    _pack_anchor_units_by_model_input,
    _split_anchor_unit_once,
    _split_anchor_unit_to_model_limit,
)
from src.worktrace.stores.markdown import MarkdownEventStore


def _config(**overrides) -> RuntimeConfig:
    return RuntimeConfig(
        use_initial_conversation_windows=False,
        self_relation_types=(
            EventMetadataItem("initiated", "发起", 10),
        ),
        **overrides,
    )


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
        self_evidence_message_ids=[source_message_id],
        self_relations=[SelfRelationEvidence("initiated", [source_message_id])],
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
        self.analysis_batches = []

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
        self.analysis_batches.append(batch)
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
    config = _config(data_root=tmp_path / "data")
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


def test_runner_filters_segment_candidate_without_validated_self_relation(
    tmp_path: Path,
) -> None:
    class MissingRelationAnalyzer(SegmentBatchAnalyzer):
        def analyze_segment_batch(self, batch):
            unit = batch.segments[0]
            candidate = replace(
                _candidate("missing-relation", unit.primary_message_ids[0]),
                self_evidence_message_ids=[],
                self_relations=[],
            )
            return BatchSegmentAnalysisResult(
                results=[
                    BatchSegmentAnalysisItem(
                        unit.segment_id,
                        BatchAnalysisResult(candidate_events=[candidate]),
                    )
                ]
            )

    config = _config(data_root=tmp_path / "data")
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=SegmentSource(),
            content_resolver=SegmentResolver(),
            analyzer=MissingRelationAnalyzer(),
            delivery_channel=SegmentDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-07-10")

    assert result.status == DailyRunStatus.SUCCESS_WITH_WARNINGS.value
    assert result.event_count == 0
    assert "Filtered candidate without validated self relation" in result.error_summary


def test_runner_does_not_summarize_images_before_segment_batch_analysis(
    tmp_path: Path,
) -> None:
    class DebugResolver(SegmentResolver):
        def summarize_images(self, messages):
            raise AssertionError("images must be summarized only after a context request")

    debug_root = tmp_path / "debug"
    config = _config(data_root=tmp_path / "data", conversation_debug_root=debug_root)
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=SegmentSource(),
            content_resolver=DebugResolver(),
            analyzer=SegmentBatchAnalyzer(),
            delivery_channel=SegmentDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-07-10")

    assert result.status == DailyRunStatus.SUCCESS.value
    batch_dirs = list((debug_root / "2026-07-10" / "_segment_batches").glob("**/analysis-01"))
    assert batch_dirs
    input_payload = json.loads((batch_dirs[0] / "input.json").read_text(encoding="utf-8"))
    assert "图片内容摘要" not in json.dumps(input_payload, ensure_ascii=False)
    assert (batch_dirs[0] / "output.json").is_file()
    assert (batch_dirs[0] / "candidate_validation.json").is_file()


def test_runner_does_not_eagerly_summarize_images(tmp_path: Path) -> None:
    class OrderingResolver(SegmentResolver):
        def __init__(self) -> None:
            self.message_ids: list[str] = []

        def summarize_images(self, messages):
            self.message_ids = [item.message_id for item in messages]
            return []

    resolver = OrderingResolver()
    runner = DailyTraceRunner(
        config=_config(data_root=tmp_path / "data"),
        dependencies=RuntimeDependencies(
            chat_source=SegmentSource(),
            content_resolver=resolver,
            analyzer=SegmentBatchAnalyzer(),
            delivery_channel=SegmentDelivery(),
            event_store=MarkdownEventStore(config=_config(data_root=tmp_path / "data")),
        ),
    )

    result = runner.run("2026-07-10")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert resolver.message_ids == []


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

    config = _config(data_root=tmp_path / "data", anchor_retry_limit=1)
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


def test_runner_dumps_failed_segmentation_attempt_before_retry(tmp_path: Path) -> None:
    class ProtocolRetryAnalyzer(SegmentBatchAnalyzer):
        def segment_conversation(self, **kwargs):
            self.segmentation_calls += 1
            if self.segmentation_calls == 1:
                raise AnalyzerProtocolError("temporary segmentation failure")
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

    debug_root = tmp_path / "debug"
    config = _config(
        data_root=tmp_path / "data",
        conversation_debug_root=debug_root,
        anchor_retry_limit=1,
    )
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=SegmentSource(),
            content_resolver=SegmentResolver(),
            analyzer=ProtocolRetryAnalyzer(),
            delivery_channel=SegmentDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-07-10")

    assert result.status == DailyRunStatus.SUCCESS.value, result.error_summary
    failure_paths = list(
        debug_root.glob("2026-07-10/_segment_batches/**/segmentation-01/failure.json")
    )
    output_paths = list(
        debug_root.glob(
            "2026-07-10/_segment_batches/**/segmentation-02/segmentation_output.json"
        )
    )
    assert len(failure_paths) == 1
    assert len(output_paths) == 1
    failure = json.loads(failure_paths[0].read_text(encoding="utf-8"))
    assert failure["stage"] == "segmentation"
    assert failure["attempt"] == 1


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
    config = _config(data_root=tmp_path / "data")
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


def test_runner_limits_parallel_segmentation_and_waits_for_its_phase(
    tmp_path: Path,
) -> None:
    class MultiConversationSource(SegmentSource):
        def list_target_conversations(self, target_date, self_identity):
            return [
                ConversationRef(conversation_id=f"oc_{index}", conversation_name=f"项目{index}")
                for index in range(1, 7)
            ]

        def fetch_conversation_messages(self, target_date, conversation_ids):
            return [
                replace(
                    _message(
                        f"om_{index}",
                        sender_open_id="ou_self",
                        minute=index,
                        text=f"推进事项 {index}",
                    ),
                    conversation_id=f"oc_{index}",
                    conversation_name=f"项目{index}",
                )
                for index in range(1, 7)
            ]

    class ParallelAnalyzer:
        def __init__(self) -> None:
            self.lock = Lock()
            self.active_segmentations = 0
            self.peak_segmentations = 0
            self.completed_segmentations = 0
            self.analysis_started_before_all_segmentations = False
            self.active_event_extractions = 0
            self.peak_event_extractions = 0

        def segment_conversation(self, **kwargs):
            with self.lock:
                self.active_segmentations += 1
                self.peak_segmentations = max(
                    self.peak_segmentations, self.active_segmentations
                )
            sleep(0.04)
            with self.lock:
                self.active_segmentations -= 1
                self.completed_segmentations += 1
            message_id = kwargs["messages"][0].message_id
            return ConversationSegmentationResult(
                segments=[
                    ConversationSegment(
                        segment_id="turn",
                        primary_message_ids=[message_id],
                        self_evidence_message_ids=[message_id],
                    )
                ]
            )

        def analyze_segment_batch(self, batch):
            with self.lock:
                self.analysis_started_before_all_segmentations |= (
                    self.completed_segmentations != 6
                )
                self.active_event_extractions += 1
                self.peak_event_extractions = max(
                    self.peak_event_extractions, self.active_event_extractions
                )
            sleep(0.04)
            with self.lock:
                self.active_event_extractions -= 1
            return BatchSegmentAnalysisResult(
                results=[
                    BatchSegmentAnalysisItem(
                        item.segment_id,
                        BatchAnalysisResult(
                            candidate_events=[
                                _candidate(
                                    f"{item.segment_id}:{item.primary_message_ids[0]}",
                                    item.primary_message_ids[0],
                                )
                            ]
                        ),
                    )
                    for item in batch.segments
                ]
            )

        def merge_day_candidates(self, target_date, candidates):
            return CrossConversationGroupResult(
                groups=[
                    CrossConversationGroup(group_id=item.draft_id, draft_ids=[item.draft_id])
                    for item in candidates
                ]
            )

    analyzer = ParallelAnalyzer()
    config = _config(
        data_root=tmp_path / "data",
        max_concurrent_llm_requests=3,
        max_concurrent_event_extraction_requests=5,
    )
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=MultiConversationSource(),
            content_resolver=SegmentResolver(),
            analyzer=analyzer,
            delivery_channel=SegmentDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-07-10")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert analyzer.peak_segmentations == 3
    assert analyzer.peak_event_extractions == 5
    assert not analyzer.analysis_started_before_all_segmentations


def test_runner_prioritizes_larger_inputs_for_segmentation_and_event_extraction(
    tmp_path: Path,
) -> None:
    class OrderedSource(SegmentSource):
        def list_target_conversations(self, target_date, self_identity):
            return [
                ConversationRef(conversation_id="oc_a_small", conversation_name="小事项"),
                ConversationRef(conversation_id="oc_z_large", conversation_name="大事项"),
            ]

        def fetch_conversation_messages(self, target_date, conversation_ids):
            return [
                replace(
                    _message("om_small", sender_open_id="ou_self", minute=0, text="简短消息"),
                    conversation_id="oc_a_small",
                    conversation_name="小事项",
                ),
                replace(
                    _message(
                        "om_large",
                        sender_open_id="ou_self",
                        minute=1,
                        text="较长消息" * 200,
                    ),
                    conversation_id="oc_z_large",
                    conversation_name="大事项",
                ),
            ]

    class OrderedAnalyzer:
        def __init__(self) -> None:
            self.segmentation_order: list[str] = []
            self.event_extraction_order: list[str] = []

        def segment_conversation(self, **kwargs):
            message_id = kwargs["messages"][0].message_id
            self.segmentation_order.append(message_id)
            return ConversationSegmentationResult(
                segments=[
                    ConversationSegment(
                        segment_id="turn",
                        primary_message_ids=[message_id],
                        self_evidence_message_ids=[message_id],
                    )
                ]
            )

        def analyze_segment_batch(self, batch):
            message_id = batch.segments[0].primary_message_ids[0]
            self.event_extraction_order.append(message_id)
            return BatchSegmentAnalysisResult(
                results=[
                    BatchSegmentAnalysisItem(
                        unit.segment_id,
                        BatchAnalysisResult(
                            candidate_events=[_candidate(message_id, message_id)]
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

    analyzer = OrderedAnalyzer()
    config = _config(
        data_root=tmp_path / "data",
        max_concurrent_llm_requests=1,
        max_concurrent_event_extraction_requests=1,
        model_input_batch_target_tokens=6_200,
        prompt_message_char_limit=2_000,
    )
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=OrderedSource(),
            content_resolver=SegmentResolver(),
            analyzer=analyzer,
            delivery_channel=SegmentDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-07-10")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert analyzer.segmentation_order == ["om_large", "om_small"]
    assert analyzer.event_extraction_order == ["om_large", "om_small"]


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

    config = _config(data_root=tmp_path / "data")
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
            self.analysis_batches.append(batch)
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

    probe_config = _config(
        data_root=tmp_path / "probe-data",
        model_input_batch_target_tokens=100_000,
    )
    probe_analyzer = SplitAnalyzer()
    probe_runner = DailyTraceRunner(
        config=probe_config,
        dependencies=RuntimeDependencies(
            chat_source=SegmentSource(),
            content_resolver=SegmentResolver(),
            analyzer=probe_analyzer,
            delivery_channel=SegmentDelivery(),
            event_store=MarkdownEventStore(config=probe_config),
        ),
    )

    probe_result = probe_runner.run("2026-07-10")

    assert probe_result.status == DailyRunStatus.SUCCESS.value
    assert len(probe_analyzer.analysis_batches) == 1
    combined_batch = probe_analyzer.analysis_batches[0]
    combined_tokens = _estimate_segment_batch_tokens(combined_batch, probe_config)
    single_tokens = max(
        _estimate_segment_batch_tokens(
            replace(combined_batch, segments=[unit]),
            probe_config,
        )
        for unit in combined_batch.segments
    )
    assert single_tokens < combined_tokens

    config = _config(
        data_root=tmp_path / "data",
        model_input_batch_target_tokens=single_tokens,
    )
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


def test_runner_does_not_retry_model_input_rejection(tmp_path: Path) -> None:
    class RejectingAnalyzer(SegmentBatchAnalyzer):
        def analyze_segment_batch(self, batch):
            self.batch_calls += 1
            raise ModelInputRejectedError("HTTP 400: model input rejected")

    config = _config(
        data_root=tmp_path / "data",
        model_input_batch_target_tokens=100_000,
    )
    analyzer = RejectingAnalyzer()
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

    assert result.status == DailyRunStatus.FAILED.value
    assert result.output_path is None
    assert analyzer.batch_calls == 1


def test_segmentation_window_is_split_until_complete_inputs_fit_limit() -> None:
    messages = [
        _message(
            f"om_split_{index}",
            sender_open_id="ou_self",
            minute=index,
            text=f"事项{index}" * 700,
        )
        for index in range(4)
    ]
    anchor = AnchorUnit(
        anchor_unit_id="oversized-window",
        conversation_id="oc_1",
        conversation_name="项目群",
        anchor_message_ids=[item.message_id for item in messages],
        in_day_message_ids=[item.message_id for item in messages],
        base_message_ids=[item.message_id for item in messages],
        messages=messages,
    )
    identity = SelfIdentity("ou_self", "张宝华", "test")
    probe_config = _config(
        model_input_batch_target_tokens=100_000,
        prompt_message_char_limit=3_000,
    )

    def estimate(item: AnchorUnit) -> int:
        return _estimate_segmentation_input_tokens(
            target_date="2026-07-10",
            anchor_unit=item,
            self_identity=identity,
            config=probe_config,
            reaction_catalog=ReactionCatalog.empty("test"),
        )

    first_split = _split_anchor_unit_once(anchor)
    input_limit = max(estimate(item) for item in first_split)
    assert estimate(anchor) > input_limit

    parts = _split_anchor_unit_to_model_limit(
        anchor,
        estimate=estimate,
        input_limit=input_limit,
    )

    assert len(parts) == 2
    assert all(estimate(item) <= input_limit for item in parts)
    assert {
        message_id
        for item in parts
        for message_id in item.base_message_ids
    } == {message.message_id for message in messages}


def test_anchor_fallback_batches_are_repacked_by_complete_input_limit() -> None:
    anchors = [_anchor_unit(index) for index in range(1, 4)]
    probe_config = _config(model_input_batch_target_tokens=100_000)
    input_limit = _estimate_anchor_batch_input_tokens(
        "2026-07-10",
        anchors[:2],
        probe_config,
    )
    config = replace(probe_config, model_input_batch_target_tokens=input_limit)
    assert (
        _estimate_anchor_batch_input_tokens("2026-07-10", anchors, config)
        > input_limit
    )

    batches = _pack_anchor_units_by_model_input(
        target_date="2026-07-10",
        anchor_units=anchors,
        config=config,
        max_batch_size=3,
    )

    assert [len(batch) for batch in batches] == [2, 1]
    assert all(
        _estimate_anchor_batch_input_tokens("2026-07-10", batch, config)
        <= input_limit
        for batch in batches
    )


def test_cross_conversation_merge_reconciles_token_limited_batches(
    tmp_path: Path,
) -> None:
    candidates = [
        replace(
            _candidate(f"draft-{index}", f"om_{index}"),
            content=f"候选事项{index}" * 500,
        )
        for index in range(4)
    ]
    pair_limit = max(
        _estimate_day_merge_input_tokens("2026-07-10", candidates[:2]),
        _estimate_day_merge_input_tokens("2026-07-10", candidates[2:]),
    )
    assert (
        _estimate_day_merge_input_tokens("2026-07-10", candidates) > pair_limit
    )

    class MergeAnalyzer:
        def __init__(self) -> None:
            self.calls: list[list[SourceBackedEventDraft]] = []

        def merge_day_candidates(self, target_date, batch):
            self.calls.append(list(batch))
            if all(item.draft_id.startswith("__cross_batch_summary_") for item in batch):
                return CrossConversationGroupResult(
                    groups=[
                        CrossConversationGroup(
                            group_id="summary-group",
                            draft_ids=[item.draft_id for item in batch],
                            primary_draft_id=batch[0].draft_id,
                        )
                    ]
                )
            return CrossConversationGroupResult(
                groups=[
                    CrossConversationGroup(
                        group_id=item.draft_id,
                        draft_ids=[item.draft_id],
                        primary_draft_id=item.draft_id,
                    )
                    for item in batch
                ]
            )

    analyzer = MergeAnalyzer()
    config = _config(
        data_root=tmp_path / "data",
        model_input_batch_target_tokens=pair_limit,
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

    result, warnings = runner._merge_day_candidates_with_batching(
        "2026-07-10",
        candidates,
    )

    assert len(analyzer.calls) == 3
    assert all(
        _estimate_day_merge_input_tokens("2026-07-10", batch) <= pair_limit
        for batch in analyzer.calls
    )
    assert result.groups[0].draft_ids == [item.draft_id for item in candidates]
    assert warnings == []


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

    debug_root = tmp_path / "debug"
    config = _config(
        data_root=tmp_path / "data",
        conversation_debug_root=debug_root,
        analysis_batch_retry_limit=1,
    )
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
    analysis_failures = list(
        debug_root.glob("2026-07-10/_segment_batches/**/analysis-*/failure.json")
    )
    fallback_outputs = list(
        debug_root.glob("2026-07-10/_segment_batches/**/fallback-01/output.json")
    )
    fallback_failures = list(
        debug_root.glob("2026-07-10/_segment_batches/**/fallback-01/failure.json")
    )
    assert len(analysis_failures) == 2
    assert len(fallback_outputs) == 1
    assert len(fallback_failures) == 1
    assert all(
        json.loads(path.read_text(encoding="utf-8"))["stage"] == "segment_batch"
        for path in analysis_failures
    )
    fallback_failure = json.loads(fallback_failures[0].read_text(encoding="utf-8"))
    assert fallback_failure["stage"] == "segment_fallback"


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

    debug_root = tmp_path / "debug"
    config = _config(
        data_root=tmp_path / "data",
        conversation_debug_root=debug_root,
        anchor_retry_limit=0,
    )
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
    fallback_outputs = list(
        debug_root.glob("2026-07-10/_anchor_fallback/**/attempt-01/output.json")
    )
    fallback_validations = list(
        debug_root.glob("2026-07-10/_anchor_fallback/**/attempt-01/validation.json")
    )
    assert len(fallback_outputs) == 1
    assert len(fallback_validations) == 1


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
    config = _config(
        data_root=tmp_path / "data",
        conversation_debug_root=tmp_path / "debug",
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
    failure_paths = list(
        (tmp_path / "debug").glob(
            "2026-07-10/_anchor_fallback/**/attempt-01/failure.json"
        )
    )
    output_paths = list(
        (tmp_path / "debug").glob(
            "2026-07-10/_anchor_fallback/**/attempt-02/output.json"
        )
    )
    assert len(failure_paths) == 1
    assert len(output_paths) == 1


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
    config = _config(
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
    config = _config(
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
    config = _config(data_root=tmp_path / "data", anchor_batch_size=2)
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
    config = _config(data_root=tmp_path / "data", anchor_batch_size=2)
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
    config = _config(
        data_root=tmp_path / "data",
        anchor_retry_limit=0,
        conversation_segmentation_failure_threshold=2,
        anchor_batch_size=3,
        max_concurrent_llm_requests=3,
        model_input_batch_target_tokens=100_000,
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
    assert [
        anchor_id
        for batch in analyzer.anchor_batches
        for anchor_id in batch
    ] == ["oc_1:om_005", "oc_1:om_037", "oc_1:om_070"]
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
    config = _config(
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

    config = _config(data_root=tmp_path / "data")
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

    config = _config(data_root=tmp_path / "data", anchor_retry_limit=1)
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
