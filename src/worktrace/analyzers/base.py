from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import (
    AnalysisBatch,
    AnchorUnit,
    BatchAnalysisResult,
    BatchAnchorAnalysisResult,
    BatchSegmentAnalysisResult,
    CollectedMergeResult,
    CollectedSourceEvent,
    ConversationSegmentationResult,
    ConversationSegmentUnit,
    CrossConversationGroupResult,
    SourceBackedEventDraft,
    SegmentAnalysisBatch,
    NormalizedMessage,
    ResponseSignal,
)


class Analyzer(ABC):
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
