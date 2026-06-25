from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import (
    AnalysisBatch,
    AnchorUnit,
    BatchAnalysisResult,
    BatchAnchorAnalysisResult,
    BucketMergedDraft,
    CrossConversationGroupResult,
    CrossBucketMergeResult,
    CrossMergeBucketResult,
    MergedEventDraft,
    SourceBackedEventDraft,
)


class Analyzer(ABC):
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
    def bucket_cross_merge_candidates(
        self,
        target_date: str,
        candidates: list[SourceBackedEventDraft],
    ) -> CrossMergeBucketResult:
        raise NotImplementedError

    @abstractmethod
    def decide_cross_bucket_merges(
        self,
        target_date: str,
        merged_buckets: list[BucketMergedDraft],
        candidate_pairs: list[tuple[str, str]],
    ) -> CrossBucketMergeResult:
        raise NotImplementedError
