from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from src.worktrace.config import RuntimeConfig, load_runtime_config_overrides
from src.worktrace.constants import DailyRunStatus
from src.worktrace.factories import RuntimeDependencies
from src.worktrace.models import (
    BatchAnalysisResult,
    ConversationRef,
    NormalizedMessage,
    RetentionReviewItemResult,
    RetentionReviewResult,
    RetentionSignalEvidence,
    SelfIdentity,
    SourceBackedEventDraft,
)
from src.worktrace.runner import DailyTraceRunner
from src.worktrace.stores.markdown import MarkdownEventStore


BASE_CONFIG = load_runtime_config_overrides(RuntimeConfig(), cwd=Path.cwd())


class ReviewSource:
    def get_self_identity(self):
        return SelfIdentity(open_id="ou_self", display_name="测试用户", source="fake")

    def list_target_conversations(self, target_date, self_identity):
        return [ConversationRef(conversation_id="oc_1", conversation_name="协作群")]

    def fetch_conversation_messages(self, target_date, conversation_ids):
        return [
            NormalizedMessage(
                conversation_id="oc_1",
                conversation_name="协作群",
                message_id="m1",
                sender_open_id="ou_other",
                sender_name="同事",
                send_time="2026-07-15T09:00:00+08:00",
                message_type="text",
                text="帮我确认一下相关同事是否在工位",
                reply_to_message_id=None,
                quote_message_id=None,
                links=[],
                attachments=[],
                is_system=False,
            ),
            NormalizedMessage(
                conversation_id="oc_1",
                conversation_name="协作群",
                message_id="m2",
                sender_open_id="ou_self",
                sender_name="测试用户",
                send_time="2026-07-15T09:01:00+08:00",
                message_type="text",
                text="我还在路上，稍后再看",
                reply_to_message_id="m1",
                quote_message_id=None,
                links=[],
                attachments=[],
                is_system=False,
            ),
        ]

    def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
        return []


class ReviewResolver:
    def to_text(self, message):
        return message.text

    def extract_links(self, message):
        return list(message.links)

    def load_attachment_text_if_needed(self, message, attachment_ids, hint):
        return None


class ReviewDelivery:
    def deliver_to_self(self, *, self_identity, markdown_path):
        return ("success", self_identity.open_id)


class ReviewAnalyzer:
    def __init__(self, result: RetentionReviewResult):
        self.result = result
        self.review_calls = 0

    def build_batch_prompt(self, batch_input):
        return "batch prompt"

    def analyze_batch(self, target_date, batch_input):
        return BatchAnalysisResult(
            candidate_events=[
                SourceBackedEventDraft(
                    draft_id="d1",
                    date=target_date,
                    topic="协助确认同事工作状态",
                    content="响应同事请求并反馈当前状态。",
                    source_message_ids=["m1", "m2"],
                    source_conversation_id="oc_1",
                    source_slice_id=batch_input.slices[0].slice_id,
                    confidence=0.9,
                    action_label="确认",
                    object_hint="相关同事",
                    retention_reason="follow_up_assigned",
                    retention_detail="响应同事请求并完成信息反馈。",
                    self_evidence_message_ids=["m2"],
                )
            ],
            context_requests=[],
        )

    def review_retention_candidates(self, batch):
        self.review_calls += 1
        return self.result

    def merge_day_candidates(self, target_date, candidates, *, validation_feedback=""):
        raise AssertionError("A single retained candidate must not call merge")


def _runner(tmp_path: Path, analyzer: ReviewAnalyzer, *, retry_limit: int = 1):
    config = replace(
        BASE_CONFIG,
        data_root=tmp_path / "data",
        conversation_debug_root=tmp_path / "debug",
        analysis_batch_retry_limit=retry_limit,
    )
    return DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=ReviewSource(),
            content_resolver=ReviewResolver(),
            analyzer=analyzer,
            delivery_channel=ReviewDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )


def test_runner_drops_source_backed_routine_coordination(tmp_path: Path) -> None:
    analyzer = ReviewAnalyzer(
        RetentionReviewResult(
            results=[
                RetentionReviewItemResult(
                    draft_id="d1",
                    routine_signals=[
                        RetentionSignalEvidence(
                            "presence_or_availability",
                            ["m1", "m2"],
                        )
                    ],
                )
            ]
        )
    )

    result = _runner(tmp_path, analyzer).run("2026-07-15")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert result.event_count == 0
    assert result.retention_review_summary.to_dict() == {
        "selected_candidate_count": 1,
        "reviewed_candidate_count": 1,
        "kept_candidate_count": 0,
        "dropped_routine_count": 1,
        "dropped_uncertain_count": 0,
        "review_batch_count": 1,
        "review_retry_count": 0,
    }
    assert analyzer.review_calls == 1
    debug_payload = json.loads(
        (tmp_path / "debug" / "2026-07-15" / "retention_review.json").read_text(
            encoding="utf-8"
        )
    )
    attempt = debug_payload["batches"][0]
    assert attempt["status"] == "success"
    assert attempt["candidates"][0]["before"]["topic"] == "协助确认同事工作状态"
    assert attempt["coverage"]["d1"]["routine_evidence_message_ids"] == [
        "m1",
        "m2",
    ]


def test_runner_drops_semantically_uncertain_boundary_candidate(
    tmp_path: Path,
) -> None:
    analyzer = ReviewAnalyzer(
        RetentionReviewResult(
            results=[RetentionReviewItemResult(draft_id="d1")]
        )
    )

    result = _runner(tmp_path, analyzer).run("2026-07-15")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert result.event_count == 0
    assert result.retention_review_summary.dropped_uncertain_count == 1


def test_runner_keeps_candidate_when_substantive_signal_exists(tmp_path: Path) -> None:
    analyzer = ReviewAnalyzer(
        RetentionReviewResult(
            results=[
                RetentionReviewItemResult(
                    draft_id="d1",
                    routine_signals=[
                        RetentionSignalEvidence(
                            "presence_or_availability",
                            ["m1"],
                        )
                    ],
                    substantive_signals=[
                        RetentionSignalEvidence(
                            "explicit_business_follow_up",
                            ["m1", "m2"],
                        )
                    ],
                )
            ]
        )
    )

    result = _runner(tmp_path, analyzer).run("2026-07-15")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert result.event_count == 1
    assert result.retention_review_summary.kept_candidate_count == 1
    assert result.retention_review_summary.dropped_routine_count == 0


def test_runner_does_not_call_review_for_non_boundary_candidate(
    tmp_path: Path,
) -> None:
    class NonBoundaryAnalyzer(ReviewAnalyzer):
        def analyze_batch(self, target_date, batch_input):
            result = super().analyze_batch(target_date, batch_input)
            candidate = replace(
                result.candidate_events[0],
                retention_reason="decision_made",
            )
            return replace(result, candidate_events=[candidate])

        def review_retention_candidates(self, batch):
            raise AssertionError("Non-boundary candidates must not be reviewed")

    analyzer = NonBoundaryAnalyzer(RetentionReviewResult(results=[]))

    result = _runner(tmp_path, analyzer).run("2026-07-15")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert result.event_count == 1
    assert result.retention_review_summary.selected_candidate_count == 0


@pytest.mark.parametrize(
    "review_result",
    [
        RetentionReviewResult(results=[]),
        RetentionReviewResult(
            results=[
                RetentionReviewItemResult(draft_id="d1"),
                RetentionReviewItemResult(draft_id="d1"),
            ]
        ),
        RetentionReviewResult(
            results=[
                RetentionReviewItemResult(
                    draft_id="d1",
                    routine_signals=[
                        RetentionSignalEvidence(
                            "presence_or_availability",
                            ["unknown"],
                        )
                    ],
                )
            ]
        ),
    ],
)
def test_runner_fails_without_writing_after_review_protocol_retries(
    tmp_path: Path,
    review_result: RetentionReviewResult,
) -> None:
    analyzer = ReviewAnalyzer(review_result)

    result = _runner(tmp_path, analyzer, retry_limit=1).run("2026-07-15")

    assert result.status == DailyRunStatus.FAILED.value
    assert result.output_path is None
    assert analyzer.review_calls == 2
    assert not (tmp_path / "data" / "2026" / "07").exists()


def test_runner_sends_concrete_validation_error_on_retention_review_retry(
    tmp_path: Path,
) -> None:
    class RetryingReviewAnalyzer(ReviewAnalyzer):
        def __init__(self) -> None:
            super().__init__(RetentionReviewResult())
            self.review_batches = []

        def review_retention_candidates(self, batch):
            self.review_calls += 1
            self.review_batches.append(batch)
            evidence_message_ids = ["unknown"] if self.review_calls == 1 else ["m1"]
            return RetentionReviewResult(
                results=[
                    RetentionReviewItemResult(
                        draft_id="d1",
                        routine_signals=[
                            RetentionSignalEvidence(
                                "presence_or_availability",
                                evidence_message_ids,
                            )
                        ],
                    )
                ]
            )

    analyzer = RetryingReviewAnalyzer()

    result = _runner(tmp_path, analyzer, retry_limit=1).run("2026-07-15")

    expected_error = (
        "Retention review returned an invalid signal or evidence reference."
    )
    assert result.status == DailyRunStatus.SUCCESS.value
    assert analyzer.review_calls == 2
    assert analyzer.review_batches[0].retry_feedback == ""
    assert analyzer.review_batches[1].retry_feedback == expected_error
    debug_payload = json.loads(
        (tmp_path / "debug" / "2026-07-15" / "retention_review.json").read_text(
            encoding="utf-8"
        )
    )
    assert debug_payload["batches"][1]["retry_feedback"] == expected_error
