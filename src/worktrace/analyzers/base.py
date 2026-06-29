from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import (
    AnalysisBatch,
    AnchorUnit,
    BatchAnalysisResult,
    BatchAnchorAnalysisResult,
    CollectedMergeResult,
    CollectedSourceEvent,
    CrossConversationGroupResult,
    SourceBackedEventDraft,
)


class Analyzer(ABC):
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
