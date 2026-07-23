from __future__ import annotations

from dataclasses import dataclass
from threading import local
from time import perf_counter
from typing import Any

from ..errors import (
    AnalyzerProtocolError,
    ModelInputRejectedError,
    RetryableAnalyzerProtocolError,
)
from ..llm_usage import LLMUsageRecorder
from ..models import (
    AnalysisBatch,
    AnchorUnit,
    AttachmentTextBlock,
    BatchAnalysisResult,
    BatchAnchorAnalysisResult,
    BatchSegmentAnalysisResult,
    CollectedGroupingGroup,
    CollectedGroupingResult,
    CollectedMergeResult,
    CollectedSourceEvent,
    ConversationSegmentationResult,
    CrossConversationGroupResult,
    NormalizedMessage,
    PersonalFactReviewBatch,
    PersonalFactReviewResult,
    ResponseSignal,
    RetentionReviewBatch,
    RetentionReviewResult,
    SegmentAnalysisBatch,
    SourceBackedEventDraft,
)
from .base import Analyzer
from .function_calls import FunctionCallSpec


_REQUEST_KINDS = {
    "analyze_batch": "batch_analysis",
    "segment_conversation": "conversation_segmentation",
    "analyze_segment_batch": "segment_batch_analysis",
    "review_retention_candidates": "retention_review",
    "review_personal_event_facts": "personal_fact_review",
    "analyze_anchor_batch": "anchor_batch_analysis",
    "merge_day_candidates": "day_candidate_merge",
    "merge_collected_events": "collected_event_merge",
    "group_collected_events": "collected_candidate_grouping",
    "review_collected_group": "collected_group_review",
}


def _safe_error_category(error: Exception) -> str:
    if isinstance(error, ModelInputRejectedError):
        return "request_rejected"
    message = str(error).lower()
    if "429" in message or "rate limit" in message:
        return "rate_limited"
    if "401" in message or "authentication" in message:
        return "authentication"
    if "403" in message or "permission" in message:
        return "permission"
    if "certificate" in message or "tls" in message:
        return "tls"
    if "timed out" in message or "timeout" in message:
        return "timeout"
    if "network" in message or "connection" in message:
        return "network"
    if "stream" in message and "json" in message:
        return "stream_json"
    if "did not contain text" in message:
        return "empty_response"
    if "json" in message:
        return "invalid_json"
    return "invalid_protocol"


def _input_metrics(error: Exception) -> dict[str, int | bool | None]:
    return {
        "estimated_input_tokens": getattr(error, "estimated_input_tokens", None),
        "input_target_tokens": getattr(error, "input_target_tokens", None),
        "oversized_singleton": bool(
            getattr(error, "oversized_singleton", False)
        ),
    }


@dataclass
class FailoverAnalyzer(Analyzer):
    """Use Codex only for the online request that failed safely to retry."""

    primary: Analyzer
    fallback: Analyzer
    usage_recorder: LLMUsageRecorder

    def __post_init__(self) -> None:
        self._request_state = local()

    def last_request_used_fallback(self) -> bool:
        return bool(getattr(self._request_state, "used_fallback", False))

    def _call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        self._request_state.used_fallback = False
        started_at = perf_counter()
        function_spec = kwargs.get("function_spec")
        request_kind = (
            function_spec.request_kind
            if isinstance(function_spec, FunctionCallSpec)
            else _REQUEST_KINDS[method_name]
        )
        try:
            return getattr(self.primary, method_name)(*args, **kwargs)
        except RetryableAnalyzerProtocolError as exc:
            self._request_state.used_fallback = True
            self.usage_recorder.record(
                request_kind,
                {},
                duration_ms=(perf_counter() - started_at) * 1000,
                backend="online",
                status="failed",
                fallback_from="online",
                fallback_to="codex",
                error_category=_safe_error_category(exc),
                **_input_metrics(exc),
            )
            return getattr(self.fallback, method_name)(*args, **kwargs)
        except AnalyzerProtocolError as exc:
            self.usage_recorder.record(
                request_kind,
                {},
                duration_ms=(perf_counter() - started_at) * 1000,
                backend="online",
                status="failed",
                error_category=_safe_error_category(exc),
                **_input_metrics(exc),
            )
            raise

    def fallback_current_request(
        self,
        method_name: str,
        *args: Any,
        failed_request_context_id: str,
        error_category: str,
        **kwargs: Any,
    ) -> Any:
        """Send one already-retried request to Codex without changing later routing."""
        if method_name not in _REQUEST_KINDS:
            raise ValueError(f"Unsupported fallback analyzer method: {method_name}.")
        self._request_state.used_fallback = True
        self.usage_recorder.mark_request_fallback(
            failed_request_context_id,
            fallback_from="online",
            fallback_to="codex",
            error_category=error_category,
        )
        return getattr(self.fallback, method_name)(*args, **kwargs)

    def request_function(
        self,
        prompt: str,
        *,
        function_spec: FunctionCallSpec,
        allow_oversized_input: bool = False,
    ) -> object:
        return self._call(
            "request_function",
            prompt,
            function_spec=function_spec,
            allow_oversized_input=allow_oversized_input,
        )

    def build_segmentation_prompt(self, **kwargs: Any) -> str:
        return self.primary.build_segmentation_prompt(**kwargs)

    def segment_conversation(
        self,
        *,
        target_date: str,
        conversation_id: str,
        conversation_name: str,
        messages: list[NormalizedMessage],
        self_open_id: str,
        self_display_name: str,
        response_signals: list[ResponseSignal],
        hard_boundary_before_ids: set[str],
        attachment_texts: list[AttachmentTextBlock] | None = None,
        allow_oversized_input: bool = False,
    ) -> ConversationSegmentationResult:
        return self._call(
            "segment_conversation",
            target_date=target_date,
            conversation_id=conversation_id,
            conversation_name=conversation_name,
            messages=messages,
            self_open_id=self_open_id,
            self_display_name=self_display_name,
            response_signals=response_signals,
            hard_boundary_before_ids=hard_boundary_before_ids,
            attachment_texts=attachment_texts,
            allow_oversized_input=allow_oversized_input,
        )

    def build_segment_batch_prompt(self, batch: SegmentAnalysisBatch) -> str:
        return self.primary.build_segment_batch_prompt(batch)

    def analyze_segment_batch(self, batch: SegmentAnalysisBatch) -> BatchSegmentAnalysisResult:
        return self._call("analyze_segment_batch", batch)

    def build_retention_review_prompt(self, batch: RetentionReviewBatch) -> str:
        return self.primary.build_retention_review_prompt(batch)

    def review_retention_candidates(
        self, batch: RetentionReviewBatch
    ) -> RetentionReviewResult:
        return self._call("review_retention_candidates", batch)

    def build_personal_fact_review_prompt(self, batch: PersonalFactReviewBatch) -> str:
        return self.primary.build_personal_fact_review_prompt(batch)

    def review_personal_event_facts(
        self, batch: PersonalFactReviewBatch
    ) -> PersonalFactReviewResult:
        return self._call("review_personal_event_facts", batch)

    def build_batch_prompt(self, batch_input: AnalysisBatch) -> str:
        return self.primary.build_batch_prompt(batch_input)

    def build_merge_prompt(
        self, target_date: str, candidates: list[SourceBackedEventDraft]
    ) -> str:
        return self.primary.build_merge_prompt(target_date, candidates)

    def analyze_batch(
        self, target_date: str, batch_input: AnalysisBatch
    ) -> BatchAnalysisResult:
        return self._call("analyze_batch", target_date, batch_input)

    def analyze_anchor_batch(
        self, target_date: str, anchor_units: list[AnchorUnit]
    ) -> BatchAnchorAnalysisResult:
        return self._call("analyze_anchor_batch", target_date, anchor_units)

    def merge_day_candidates(
        self, target_date: str, candidates: list[SourceBackedEventDraft]
    ) -> CrossConversationGroupResult:
        return self._call("merge_day_candidates", target_date, candidates)

    def merge_collected_events(
        self,
        target_date: str,
        events: list[CollectedSourceEvent],
        deterministic_groups: list[list[str]],
    ) -> CollectedMergeResult:
        return self._call(
            "merge_collected_events", target_date, events, deterministic_groups
        )

    def group_collected_events(
        self,
        target_date: str,
        events: list[CollectedSourceEvent],
        deterministic_groups: list[list[str]],
        *,
        validation_feedback: str = "",
    ) -> CollectedGroupingResult:
        return self._call(
            "group_collected_events",
            target_date,
            events,
            deterministic_groups,
            validation_feedback=validation_feedback,
        )

    def review_collected_group(
        self,
        target_date: str,
        events: list[CollectedSourceEvent],
        candidate_group: CollectedGroupingGroup,
        *,
        review_reasons: list[str] | None = None,
        validation_feedback: str = "",
    ) -> CollectedGroupingResult:
        return self._call(
            "review_collected_group",
            target_date,
            events,
            candidate_group,
            review_reasons=review_reasons,
            validation_feedback=validation_feedback,
        )
