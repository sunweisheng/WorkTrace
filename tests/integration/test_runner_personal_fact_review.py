from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from threading import Barrier, Lock

from src.worktrace.config import RuntimeConfig, load_runtime_config_overrides
from src.worktrace.constants import DailyRunStatus
from src.worktrace.factories import RuntimeDependencies
from src.worktrace.models import (
    BatchAnalysisResult,
    ConversationSlice,
    ConversationRef,
    NormalizedMessage,
    PersonalFactItem,
    PersonalFactReviewItemResult,
    PersonalFactReviewResult,
    SelfIdentity,
    SourceBackedEventDraft,
)
from src.worktrace.runner import DailyTraceRunner
from src.worktrace.stores.markdown import MarkdownEventStore


BASE_CONFIG = load_runtime_config_overrides(RuntimeConfig(), cwd=Path.cwd())


class FactReviewSource:
    def get_self_identity(self):
        return SelfIdentity(open_id="ou_self", display_name="测试用户", source="fake")

    def list_target_conversations(self, target_date, self_identity):
        return [ConversationRef(conversation_id="oc_1", conversation_name="设备协作群")]

    def fetch_conversation_messages(self, target_date, conversation_ids):
        rows = [
            ("m1", "请修改三台设备的发货单信息", "ou_self"),
            ("m2", "先核对设备编号再签收", "ou_executor"),
            ("m3", "其中两台编号修改没有生效", "ou_self"),
            ("m4", "我重新修改", "ou_executor"),
            ("m5", "测试后没有重置，仍归属测试网点", "ou_executor"),
            ("m6", "另一个地区之前也出现过类似情况", "ou_observer"),
            ("m7", "已经完成后台重置", "ou_executor"),
            ("m8", "验证正常，继续安排签收", "ou_self"),
        ]
        return [
            NormalizedMessage(
                conversation_id="oc_1",
                conversation_name="设备协作群",
                message_id=message_id,
                sender_open_id=sender,
                sender_name=sender,
                send_time=f"2026-07-15T09:0{index}:00+08:00",
                message_type="text",
                text=text,
                reply_to_message_id=None,
                quote_message_id=None,
                links=[],
                attachments=[],
                is_system=False,
            )
            for index, (message_id, text, sender) in enumerate(rows)
        ]

    def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
        return []


class FactReviewResolver:
    def to_text(self, message):
        return message.text

    def extract_links(self, message):
        return list(message.links)

    def load_attachment_text_if_needed(self, message, attachment_ids, hint):
        return None


class FactReviewDelivery:
    def deliver_to_self(self, *, self_identity, markdown_path):
        return ("success", self_identity.open_id)


class FactReviewAnalyzer:
    def __init__(self, result: PersonalFactReviewResult):
        self.result = result
        self.review_calls = 0
        self.review_feedback: list[str] = []

    def build_batch_prompt(self, batch_input):
        return "batch prompt"

    def analyze_batch(self, target_date, batch_input):
        return BatchAnalysisResult(
            candidate_events=[
                SourceBackedEventDraft(
                    draft_id="d1",
                    date=target_date,
                    topic="示例地区设备流程审核与修改",
                    content="重置示例地区设备归属并提出流程变更建议。",
                    source_message_ids=[f"m{index}" for index in range(1, 9)],
                    source_conversation_id="oc_1",
                    source_slice_id=batch_input.slices[0].slice_id,
                    confidence=0.9,
                    action_label="重置归属并提出流程建议",
                    object_hint="示例地区设备流程",
                    retention_reason="deliverable_updated",
                    retention_detail="发起人完成后台重置并提出流程调整。",
                    self_evidence_message_ids=["m1", "m3", "m8"],
                    fact_risk_flags=[
                        "comparison_or_example",
                        "role_or_responsibility_attribution",
                        "inferred_decision_or_recommendation",
                    ],
                )
            ],
            context_requests=[],
        )

    def review_personal_event_facts(self, batch):
        self.review_calls += 1
        self.review_feedback.append(batch.retry_feedback)
        return self.result

    def merge_day_candidates(self, target_date, candidates):
        raise AssertionError("A single fact-reviewed candidate must not call merge")


class ConcurrentFactReviewAnalyzer:
    def __init__(self) -> None:
        self.barrier = Barrier(3)
        self.lock = Lock()
        self.active_count = 0
        self.max_active_count = 0

    def review_personal_event_facts(
        self,
        batch,
    ) -> PersonalFactReviewResult:
        candidate = batch.candidates[0].candidate
        evidence_id = batch.candidates[0].allowed_evidence_message_ids[0]
        with self.lock:
            self.active_count += 1
            self.max_active_count = max(self.max_active_count, self.active_count)
        try:
            self.barrier.wait(timeout=2)
            fact_items = [
                PersonalFactItem("topic", candidate.topic, [evidence_id]),
                PersonalFactItem("content", candidate.content, [evidence_id]),
                PersonalFactItem(
                    "action_label",
                    candidate.action_label,
                    [evidence_id],
                ),
                PersonalFactItem("object_hint", candidate.object_hint, [evidence_id]),
                PersonalFactItem(
                    "retention_detail",
                    candidate.retention_detail,
                    [evidence_id],
                ),
            ]
            return PersonalFactReviewResult(
                results=[
                    PersonalFactReviewItemResult(
                        draft_id=candidate.draft_id,
                        supported=True,
                        topic=candidate.topic,
                        content=candidate.content,
                        action_label=candidate.action_label,
                        object_hint=candidate.object_hint,
                        retention_detail=candidate.retention_detail,
                        fact_items=fact_items,
                    )
                ]
            )
        finally:
            with self.lock:
                self.active_count -= 1


def _corrected_result() -> PersonalFactReviewResult:
    topic = "三台设备发货信息修改及归属重置"
    content = "修改三台设备的发货单信息，重新处理未生效的编号，并在后台重置测试网点归属后继续安排签收。"
    action = "修改并核对"
    object_hint = "三台设备发货单"
    detail = "发起人反馈编号修改未生效，执行人重新修改并完成归属重置，验证后继续签收。"
    return PersonalFactReviewResult(
        results=[
            PersonalFactReviewItemResult(
                draft_id="d1",
                supported=True,
                topic=topic,
                content=content,
                action_label=action,
                object_hint=object_hint,
                retention_detail=detail,
                workstream_key="",
                fact_items=[
                    PersonalFactItem("topic", topic, ["m1", "m5", "m7"]),
                    PersonalFactItem("content", content, ["m1", "m3", "m4", "m5", "m7", "m8"]),
                    PersonalFactItem("action_label", action, ["m4", "m7"]),
                    PersonalFactItem("object_hint", object_hint, ["m1"]),
                    PersonalFactItem("retention_detail", detail, ["m3", "m4", "m7", "m8"]),
                ],
                removed_claims=[
                    "将对比地区写成实际处理对象",
                    "将发起人写成全部操作的执行人",
                    "补充原聊天没有提出的流程建议",
                ],
            )
        ]
    )


def _runner(tmp_path: Path, analyzer: FactReviewAnalyzer) -> DailyTraceRunner:
    config = replace(
        BASE_CONFIG,
        data_root=tmp_path / "data",
        conversation_debug_root=tmp_path / "debug",
        analysis_batch_retry_limit=1,
    )
    return DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=FactReviewSource(),
            content_resolver=FactReviewResolver(),
            analyzer=analyzer,
            delivery_channel=FactReviewDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )


def test_personal_fact_review_runs_three_single_candidate_batches_concurrently(
    tmp_path: Path,
) -> None:
    analyzer = ConcurrentFactReviewAnalyzer()
    runner = _runner(tmp_path, analyzer)  # type: ignore[arg-type]
    messages = FactReviewSource().fetch_conversation_messages("2026-07-15", ["oc_1"])
    message_ids = [message.message_id for message in messages]
    conversation_slice = ConversationSlice(
        slice_id="slice-1",
        conversation_id="oc_1",
        conversation_name="设备协作群",
        anchor_message_ids=["m1"],
        in_day_message_ids=message_ids,
        messages=messages,
        primary_message_ids=message_ids,
        context_message_ids=[],
        self_evidence_message_ids=["m1"],
    )
    candidates = [
        SourceBackedEventDraft(
            draft_id=f"d{index}",
            date="2026-07-15",
            topic=f"设备事项 {index}",
            content=f"完成设备事项 {index}。",
            source_message_ids=message_ids,
            source_conversation_id="oc_1",
            source_slice_id="slice-1",
            confidence=0.9,
            action_label="完成",
            object_hint=f"设备 {index}",
            retention_reason="deliverable_updated",
            retention_detail=f"设备事项 {index} 已完成。",
            self_evidence_message_ids=["m1"],
        )
        for index in range(1, 4)
    ]

    kept, summary, call_count = runner._review_personal_event_facts(
        target_date="2026-07-15",
        candidates=candidates,
        conversation_slices=[conversation_slice],
        messages=messages,
    )

    assert [candidate.draft_id for candidate in kept] == ["d1", "d2", "d3"]
    assert analyzer.max_active_count == 3
    assert call_count == 3
    assert summary.review_batch_count == 3
    assert summary.review_retry_count == 0
    debug_payload = json.loads(
        (tmp_path / "debug" / "2026-07-15" / "personal_fact_review.json").read_text(
            encoding="utf-8"
        )
    )
    assert [item["batch_id"] for item in debug_payload["batches"]] == [
        "personal-fact-review-001",
        "personal-fact-review-002",
        "personal-fact-review-003",
    ]


def test_runner_rewrites_unsupported_personal_facts_before_daily_merge(
    tmp_path: Path,
) -> None:
    analyzer = FactReviewAnalyzer(_corrected_result())

    result = _runner(tmp_path, analyzer).run("2026-07-15")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert result.event_count == 1
    assert analyzer.review_calls == 1
    assert result.personal_fact_review_summary.to_dict() == {
        "selected_candidate_count": 1,
        "reviewed_candidate_count": 1,
        "confirmed_candidate_count": 0,
        "revised_candidate_count": 1,
        "dropped_unsupported_count": 0,
        "review_batch_count": 1,
        "review_retry_count": 0,
    }
    content = Path(result.output_path or "").read_text(encoding="utf-8")
    assert "三台设备发货信息修改及归属重置" in content
    assert "示例地区" not in content
    assert "流程变更建议" not in content
    debug_payload = json.loads(
        (tmp_path / "debug" / "2026-07-15" / "personal_fact_review.json").read_text(
            encoding="utf-8"
        )
    )
    attempt = debug_payload["batches"][0]
    assert attempt["status"] == "success"
    assert attempt["candidates"][0]["before"]["topic"] == "示例地区设备流程审核与修改"
    assert attempt["result"]["results"][0]["topic"] == "三台设备发货信息修改及归属重置"
    assert attempt["coverage"]["d1"]["covered_fields"] == [
        "topic",
        "content",
        "action_label",
        "object_hint",
        "retention_detail",
    ]
    assert "risk_flag:comparison_or_example" in attempt["candidates"][0][
        "review_reasons"
    ]


def test_runner_fails_without_writing_after_fact_review_protocol_retries(
    tmp_path: Path,
) -> None:
    analyzer = FactReviewAnalyzer(PersonalFactReviewResult(results=[]))

    result = _runner(tmp_path, analyzer).run("2026-07-15")

    assert result.status == DailyRunStatus.FAILED.value
    assert result.output_path is None
    assert analyzer.review_calls == 2
    assert analyzer.review_feedback[0] == ""
    assert "must return every draft_id" in analyzer.review_feedback[1]
    assert not (tmp_path / "data" / "2026" / "07").exists()
    debug_payload = json.loads(
        (tmp_path / "debug" / "2026-07-15" / "personal_fact_review.json").read_text(
            encoding="utf-8"
        )
    )
    assert [item["status"] for item in debug_payload["batches"]] == [
        "failed",
        "failed",
    ]
    assert all(item["result"] == {"results": []} for item in debug_payload["batches"])
    assert "must return every draft_id" in debug_payload["error_summary"]
