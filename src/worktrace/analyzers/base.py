from __future__ import annotations

from abc import ABC, abstractmethod

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
    ConversationSegmentUnit,
    CrossConversationGroupResult,
    SourceBackedEventDraft,
    SegmentAnalysisBatch,
    NormalizedMessage,
    PersonalFactReviewBatch,
    PersonalFactReviewResult,
    ResponseSignal,
    RetentionReviewBatch,
    RetentionReviewResult,
)
from .function_calls import FunctionCallSpec


class Analyzer(ABC):
    def request_function(
        self,
        prompt: str,
        *,
        function_spec: FunctionCallSpec,
        allow_oversized_input: bool = False,
    ) -> object:
        raise NotImplementedError

    @abstractmethod
    def build_segmentation_prompt(
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
    ) -> str:
        raise NotImplementedError

    @abstractmethod
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
        raise NotImplementedError

    @abstractmethod
    def build_segment_batch_prompt(self, batch: SegmentAnalysisBatch) -> str:
        raise NotImplementedError

    @abstractmethod
    def analyze_segment_batch(
        self,
        batch: SegmentAnalysisBatch,
    ) -> BatchSegmentAnalysisResult:
        raise NotImplementedError

    def build_retention_review_prompt(self, batch: RetentionReviewBatch) -> str:
        raise NotImplementedError

    def review_retention_candidates(
        self,
        batch: RetentionReviewBatch,
    ) -> RetentionReviewResult:
        raise NotImplementedError

    def build_personal_fact_review_prompt(
        self,
        batch: PersonalFactReviewBatch,
    ) -> str:
        raise NotImplementedError

    def review_personal_event_facts(
        self,
        batch: PersonalFactReviewBatch,
    ) -> PersonalFactReviewResult:
        raise NotImplementedError

    @abstractmethod
    def build_batch_prompt(self, batch_input: AnalysisBatch) -> str:
        raise NotImplementedError

    @abstractmethod
    def build_merge_prompt(
        self,
        target_date: str,
        candidates: list[SourceBackedEventDraft],
    ) -> str:
        raise NotImplementedError

    @abstractmethod
    def analyze_batch(
        self,
        target_date: str,
        batch_input: AnalysisBatch,
    ) -> BatchAnalysisResult:
        raise NotImplementedError

    @abstractmethod
    def analyze_anchor_batch(
        self,
        target_date: str,
        anchor_units: list[AnchorUnit],
    ) -> BatchAnchorAnalysisResult:
        raise NotImplementedError

    @abstractmethod
    def merge_day_candidates(
        self,
        target_date: str,
        candidates: list[SourceBackedEventDraft],
    ) -> CrossConversationGroupResult:
        raise NotImplementedError

    @abstractmethod
    def merge_collected_events(
        self,
        target_date: str,
        events: list[CollectedSourceEvent],
        deterministic_groups: list[list[str]],
    ) -> CollectedMergeResult:
        raise NotImplementedError

    @abstractmethod
    def group_collected_events(
        self,
        target_date: str,
        events: list[CollectedSourceEvent],
        deterministic_groups: list[list[str]],
        *,
        validation_feedback: str = "",
    ) -> CollectedGroupingResult:
        raise NotImplementedError

    def review_collected_group(
        self,
        target_date: str,
        events: list[CollectedSourceEvent],
        candidate_group: CollectedGroupingGroup,
        *,
        review_reasons: list[str] | None = None,
        validation_feedback: str = "",
    ) -> CollectedGroupingResult:
        raise NotImplementedError


def is_indivisible_collected_request(
    events: list[CollectedSourceEvent],
    deterministic_groups: list[list[str]],
) -> bool:
    if len(events) <= 1:
        return True
    event_ids = {item.draft_id for item in events}
    return any(set(group) == event_ids for group in deterministic_groups)


def oversized_input_kwargs(allow: bool) -> dict[str, bool]:
    return {"allow_oversized_input": True} if allow else {}
