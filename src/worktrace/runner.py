from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from hashlib import sha1
import logging
from pathlib import Path
import re
from time import perf_counter
from urllib.parse import urlsplit

from .config import RuntimeConfig
from .analyzers.base import Analyzer
from .constants import DailyRunStatus
from .errors import AnalyzerProtocolError, ChatSourceError, DeliveryError, StoreWriteError
from .factories import RuntimeDependencies, build_runtime_dependencies
from .logging_utils import log_timing
from .models import (
    AnalysisBatch,
    AnchorAnalysisResult,
    AnchorUnit,
    BatchAnalysisResult,
    BatchSegmentAnalysisResult,
    ContextRequest,
    ConversationSegmentUnit,
    CrossConversationGroup,
    DailyRunResult,
    EventFileLink,
    WorkEvent,
    MergedEventDraft,
    NormalizedMessage,
    PersonalFactReviewBatch,
    PersonalFactReviewItemResult,
    PersonalFactReviewResult,
    PersonalFactReviewSummary,
    RetentionReviewBatch,
    RetentionReviewResult,
    RetentionReviewSummary,
    SegmentAnalysisBatch,
    SourceBackedEventDraft,
    ConversationSlice,
    SelfIdentity,
    WorkstreamAssignment,
    WorkstreamAssignmentResult,
)
from .analyzers.output_schemas import workstream_assignment_output_schema
from .analyzers.prompts import (
    build_anchor_batch_analysis_prompt,
    build_unassigned_workstream_assignment_prompt,
    build_workstream_assignment_prompt,
)
from .reaction_catalog import ReactionCatalog, ReactionCatalogStore, enrich_message_reactions
from .pipeline.conversation_first_pass import build_conversation_level_slices
from .pipeline.conversation_segments import (
    build_hard_boundary_message_ids,
    build_response_signals,
    pack_segment_units,
    segment_unit_to_slice,
    validate_conversation_segmentation,
    validate_segment_batch_result,
)
from .pipeline.initial_windows import (
    append_private_window_external_relations,
    build_initial_anchor_windows,
)
from .pipeline.required_image_context import enrich_required_image_context
from .pipeline.llm_checkpoints import LLMCheckpointStore
from .pipeline.anchors import group_anchor_units
from .pipeline.anchor_expansion import expand_anchor_unit_context
from .pipeline.context_expansion import (
    build_single_slice_retry_batch,
    expand_slice_context,
)
from .pipeline.cross_conversation_merge import (
    consolidate_workstream_groups,
    materialize_grouped_merged_drafts,
)
from .pipeline.workstream_resolution import groups_from_workstream_assignments
from .pipeline.direct_relation_filter import (
    filter_candidates_with_valid_self_relations,
    filter_self_related_candidate_drafts,
)
from .pipeline.event_merge import build_work_events
from .pipeline.filtering import filter_messages
from .pipeline.retention_filter import (
    filter_retained_candidate_drafts,
    filter_retained_merged_drafts,
    filter_retained_work_events,
)
from .pipeline.retention_review import (
    apply_retention_review_results,
    build_retention_review_candidates,
    pack_retention_review_batches,
    select_retention_review_candidates,
    validate_retention_review_result,
)
from .pipeline.personal_fact_review import (
    apply_personal_fact_review_results,
    build_personal_fact_review_candidates,
    pack_personal_fact_review_batches,
    validate_personal_fact_review_result,
)
from .pipeline.sensitive_filter import (
    filter_candidate_drafts,
    filter_merged_drafts,
    filter_work_events,
)
from .pipeline.validation import (
    normalize_cross_conversation_groups_with_fallback,
    validate_batch_analysis_result,
    validate_cross_conversation_groups,
    validate_merged_event_drafts,
)
from .utils.link_refs import build_message_link_id
from .utils.hashing import file_key_from_attachment_id, file_key_from_url
from .utils.json_io import dump_json

logger = logging.getLogger("worktrace")
_LINK_TEXT_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}")
_GENERIC_LINK_HINT_TOKENS = {
    "https",
    "http",
    "www",
    "github",
    "feishu",
    "larksuite",
    "docx",
    "wiki",
    "share",
    "base",
    "form",
    "space",
    "global",
    "com",
    "cn",
}


@dataclass(frozen=True)
class _EventFileReference:
    message_id: str
    file_link: EventFileLink
    evidence_values: tuple[str, ...]


@dataclass(frozen=True)
class _AnchorSegmentationOutcome:
    units: list[ConversationSegmentUnit]
    warnings: list[str]
    error_summary: str
    retry_round: int
    failure_category: str
    model_call_count: int
    started_at: float


@dataclass
class _ConversationSegmentationState:
    conversation_id: str
    conversation_name: str
    anchors: list[AnchorUnit]
    next_anchor_index: int = 0
    circuit_open: bool = False
    fallback_required: bool = False
    skipped_anchor_count: int = 0
    units: list[ConversationSegmentUnit] = field(default_factory=list)
    cache: dict[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]], _AnchorSegmentationOutcome] = field(
        default_factory=dict
    )
    failure_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class _PersonalFactReviewBatchOutcome:
    batch: PersonalFactReviewBatch
    validated: dict[str, PersonalFactReviewItemResult]
    debug_entries: list[dict[str, object]]
    call_count: int
    retry_count: int
    error_summary: str = ""
    error_kind: str = ""


@dataclass
class DailyTraceRunner:
    config: RuntimeConfig
    dependencies: RuntimeDependencies
    reaction_catalog: ReactionCatalog | None = None
    checkpoint_store: LLMCheckpointStore | None = None

    def __post_init__(self) -> None:
        if self.reaction_catalog is None:
            source_id = getattr(self.dependencies.chat_source, "source_id", "feishu")
            self.reaction_catalog = ReactionCatalogStore.from_config(self.config).load(source_id)

    def run(self, target_date: str) -> DailyRunResult:
        run_started_at = perf_counter()
        self.checkpoint_store = LLMCheckpointStore(self.config, target_date)
        warning_messages: list[str] = []
        skipped_slice_count = 0
        retention_review_summary = RetentionReviewSummary()
        personal_fact_review_summary = PersonalFactReviewSummary()

        try:
            stage_started_at = perf_counter()
            self_identity = self.dependencies.chat_source.get_self_identity()
            log_timing(
                logger,
                "runner.stage.completed",
                stage_started_at,
                stage="get_self_identity",
                target_date=target_date,
            )
            stage_started_at = perf_counter()
            conversations = self.dependencies.chat_source.list_target_conversations(
                target_date, self_identity
            )
            log_timing(
                logger,
                "runner.stage.completed",
                stage_started_at,
                stage="list_target_conversations",
                target_date=target_date,
                conversation_count=len(conversations),
            )
            stage_started_at = perf_counter()
            messages = self.dependencies.chat_source.fetch_conversation_messages(
                target_date,
                [item.conversation_id for item in conversations],
            )
            messages = enrich_message_reactions(messages, self.reaction_catalog)
            log_timing(
                logger,
                "runner.stage.completed",
                stage_started_at,
                stage="fetch_conversation_messages",
                target_date=target_date,
                message_count=len(messages),
            )
        except ChatSourceError as exc:
            return self._finish_run(
                run_started_at,
                self._failed_result(target_date, str(exc)),
            )

        if not conversations and not messages:
            return self._finish_run(
                run_started_at,
                self._write_empty_day(
                    target_date,
                    self_identity=self_identity,
                    conversation_count=0,
                    message_count=0,
                    slice_count=0,
                    batch_count=0,
                ),
            )

        stage_started_at = perf_counter()
        filtered_messages = filter_messages(messages)
        log_timing(
            logger,
            "runner.stage.completed",
            stage_started_at,
            stage="filter_messages",
            input_message_count=len(messages),
            output_message_count=len(filtered_messages),
        )
        all_candidates: list[SourceBackedEventDraft] = []
        analyzed_batch_count = 0
        all_message_order = [message.message_id for message in filtered_messages]
        conversation_slices: list[ConversationSlice] = []

        try:
            if _supports_segment_batches(self.dependencies.analyzer):
                (
                    all_candidates,
                    conversation_slices,
                    segment_warnings,
                    skipped_slice_count,
                    analyzed_batch_count,
                ) = self._analyze_segmented_conversations(
                    target_date=target_date,
                    messages=filtered_messages,
                    self_identity=self_identity,
                )
                warning_messages.extend(segment_warnings)
            else:
                stage_started_at = perf_counter()
                conversation_slices = build_conversation_level_slices(
                    filtered_messages,
                    self_identity.open_id,
                    self.config,
                )
                log_timing(
                    logger,
                    "runner.stage.completed",
                    stage_started_at,
                    stage="build_conversation_level_slices",
                    slice_count=len(conversation_slices),
                )
                for conversation_slice in conversation_slices:
                    (
                        validated_result,
                        slice_warnings,
                        unresolved,
                        run_count,
                    ) = self._analyze_conversation_slice_with_retry(
                        target_date=target_date,
                        conversation_slice=conversation_slice,
                        self_identity=self_identity,
                    )
                    analyzed_batch_count += run_count
                    all_candidates.extend(validated_result.candidate_events)
                    if unresolved:
                        skipped_slice_count += 1
                    warning_messages.extend(slice_warnings)
        except AnalyzerProtocolError as exc:
            return self._finish_run(
                run_started_at,
                self._failed_result(target_date, str(exc)),
            )

        merged_drafts: list[MergedEventDraft] = []
        if all_candidates:
            try:
                all_candidates, candidate_filter_warnings = (
                    filter_candidate_drafts(all_candidates, self.config)
                )
                warning_messages.extend(candidate_filter_warnings)
                if not _supports_segment_batches(self.dependencies.analyzer):
                    all_candidates, self_relation_candidate_warnings = (
                        filter_self_related_candidate_drafts(
                            all_candidates,
                            {
                                item.slice_id: item
                                for item in conversation_slices
                            },
                            self_open_id=self_identity.open_id,
                            self_display_name=self_identity.display_name,
                            self_assignment_keywords=self.config.self_assignment_keywords,
                        )
                    )
                    warning_messages.extend(self_relation_candidate_warnings)
                all_candidates, retention_candidate_warnings = (
                    filter_retained_candidate_drafts(
                        all_candidates,
                        self.config.retention_policy,
                    )
                )
                warning_messages.extend(retention_candidate_warnings)
                (
                    all_candidates,
                    retention_review_summary,
                    retention_review_call_count,
                ) = self._review_retention_candidates(
                    target_date=target_date,
                    candidates=all_candidates,
                    conversation_slices=conversation_slices,
                    messages=filtered_messages,
                )
                analyzed_batch_count += retention_review_call_count
                (
                    all_candidates,
                    personal_fact_review_summary,
                    personal_fact_review_call_count,
                ) = self._review_personal_event_facts(
                    target_date=target_date,
                    candidates=all_candidates,
                    conversation_slices=conversation_slices,
                    messages=filtered_messages,
                )
                analyzed_batch_count += personal_fact_review_call_count

                if not all_candidates:
                    return self._finish_run(
                        run_started_at,
                        self._write_empty_day(
                            target_date,
                            self_identity=self_identity,
                            conversation_count=len(conversations),
                            message_count=len(messages),
                            slice_count=len(conversation_slices),
                            batch_count=analyzed_batch_count,
                            warning_messages=warning_messages,
                            skipped_slice_count=skipped_slice_count,
                            retention_review_summary=retention_review_summary,
                            personal_fact_review_summary=personal_fact_review_summary,
                        ),
                    )

                if len(all_candidates) == 1:
                    merged_drafts = materialize_grouped_merged_drafts(
                        all_candidates,
                        [
                            CrossConversationGroup(
                                group_id="single",
                                draft_ids=[all_candidates[0].draft_id],
                                primary_draft_id=all_candidates[0].draft_id,
                                workstream_name=all_candidates[0].workstream_key.strip(),
                            )
                        ],
                        target_date=target_date,
                        message_order=all_message_order,
                        self_relation_order=tuple(
                            item.key for item in self.config.self_relation_types
                        ),
                    )
                else:
                    merge_started_at = perf_counter()
                    group_result = self.dependencies.analyzer.merge_day_candidates(
                        target_date,
                        all_candidates,
                    )
                    self._dump_merge_debug_artifacts(
                        target_date=target_date,
                        candidates=all_candidates,
                        output_payload=getattr(
                            self.dependencies.analyzer,
                            "last_merge_payload",
                            None,
                        ),
                    )
                    try:
                        group_result = validate_cross_conversation_groups(
                            group_result,
                            all_candidates,
                        )
                    except AnalyzerProtocolError:
                        group_result, merge_repair_warnings = (
                            normalize_cross_conversation_groups_with_fallback(
                                group_result,
                                all_candidates,
                            )
                        )
                        warning_messages.extend(merge_repair_warnings)
                    corrected_groups, workstream_warnings = self._resolve_workstream_groups(
                        target_date=target_date,
                        model_groups=group_result.groups,
                        candidates=all_candidates,
                    )
                    warning_messages.extend(workstream_warnings)
                    group_result = replace(group_result, groups=corrected_groups)
                    self._dump_resolved_merge_groups(
                        target_date=target_date,
                        groups=group_result.groups,
                        warnings=workstream_warnings,
                    )
                    merged_drafts = materialize_grouped_merged_drafts(
                        all_candidates,
                        group_result.groups,
                        target_date=target_date,
                        message_order=all_message_order,
                        self_relation_order=tuple(
                            item.key for item in self.config.self_relation_types
                        ),
                    )
                    merged_drafts = validate_merged_event_drafts(
                        merged_drafts,
                        message_order=all_message_order,
                    )
                    log_timing(
                        logger,
                        "runner.stage.completed",
                        merge_started_at,
                        stage="merge_day_candidates",
                        candidate_event_count=len(all_candidates),
                        merged_event_count=len(merged_drafts),
                    )
            except (AnalyzerProtocolError, ValueError) as exc:
                return self._finish_run(
                    run_started_at,
                    self._failed_result(target_date, str(exc)),
                )

        if not merged_drafts:
            return self._finish_run(
                run_started_at,
                self._write_empty_day(
                    target_date,
                    self_identity=self_identity,
                    conversation_count=len(conversations),
                    message_count=len(messages),
                    slice_count=len(conversation_slices),
                    batch_count=analyzed_batch_count,
                    warning_messages=warning_messages,
                    skipped_slice_count=skipped_slice_count,
                    retention_review_summary=retention_review_summary,
                    personal_fact_review_summary=personal_fact_review_summary,
                ),
            )

        try:
            merged_drafts, merged_filter_warnings = filter_merged_drafts(
                merged_drafts,
                self.config,
            )
            warning_messages.extend(merged_filter_warnings)
            merged_drafts, retention_merged_warnings = filter_retained_merged_drafts(
                merged_drafts,
                self.config.retention_policy,
            )
            warning_messages.extend(retention_merged_warnings)
            event_build_started_at = perf_counter()
            events, merge_warnings = build_work_events(target_date, merged_drafts)
            events = _attach_event_file_links(
                events,
                messages=filtered_messages,
                content_resolver=self.dependencies.content_resolver,
            )
            events, final_event_filter_warnings = filter_work_events(events, self.config)
            warning_messages.extend(final_event_filter_warnings)
            events, retention_event_warnings = filter_retained_work_events(
                events,
                self.config.retention_policy,
            )
            warning_messages.extend(retention_event_warnings)
            events = _sort_events_for_output(events, messages=filtered_messages)
            self._dump_final_events_debug_artifacts(
                target_date=target_date,
                merged_drafts=merged_drafts,
                events=events,
                event_build_warnings=merge_warnings,
                final_filter_warnings=final_event_filter_warnings,
                retention_warnings=retention_event_warnings,
            )
            log_timing(
                logger,
                "runner.stage.completed",
                event_build_started_at,
                stage="build_work_events",
                event_count=len(events),
                warning_count=len(merge_warnings),
            )
            warning_messages.extend(merge_warnings)
            write_started_at = perf_counter()
            write_result = self.dependencies.event_store.replace_day(
                target_date,
                events,
                owner_display_name=self_identity.display_name,
            )
            if self.checkpoint_store is not None:
                self.checkpoint_store.clear()
            log_timing(
                logger,
                "runner.stage.completed",
                write_started_at,
                stage="write_markdown",
                event_count=len(events),
                output_path=write_result.output_path,
            )
            delivery_status, delivery_target, delivery_error = _deliver_markdown_to_self(
                self.dependencies.delivery_channel,
                self_identity=self_identity,
                markdown_path=Path(write_result.output_path),
            )
            if delivery_error:
                warning_messages.append(delivery_error)
        except (AnalyzerProtocolError, StoreWriteError, ValueError) as exc:
            return self._finish_run(
                run_started_at,
                self._failed_result(target_date, str(exc)),
            )

        status = (
            DailyRunStatus.SUCCESS_WITH_WARNINGS.value
            if warning_messages or skipped_slice_count
            else DailyRunStatus.SUCCESS.value
        )
        return self._finish_run(
            run_started_at,
            DailyRunResult(
                target_date=target_date,
                conversation_count=len(conversations),
                message_count=len(messages),
                slice_count=len(conversation_slices),
                batch_count=analyzed_batch_count,
                event_count=len(events),
                skipped_slice_count=skipped_slice_count,
                warning_count=len(warning_messages),
                status=status,
                output_path=write_result.output_path,
                error_summary="; ".join(warning_messages),
                self_delivery_status=delivery_status,
                self_delivery_target=delivery_target,
                self_delivery_error=delivery_error,
                retention_review_summary=retention_review_summary,
                personal_fact_review_summary=personal_fact_review_summary,
            ),
        )

    def _review_retention_candidates(
        self,
        *,
        target_date: str,
        candidates: list[SourceBackedEventDraft],
        conversation_slices: list[ConversationSlice],
        messages: list[NormalizedMessage],
    ) -> tuple[list[SourceBackedEventDraft], RetentionReviewSummary, int]:
        policy = self.config.retention_policy
        selected = select_retention_review_candidates(candidates, policy)
        if not selected:
            return candidates, RetentionReviewSummary(), 0

        review_candidates = build_retention_review_candidates(
            selected,
            slices=conversation_slices,
            messages=messages,
        )
        batches = pack_retention_review_batches(
            target_date=target_date,
            candidates=review_candidates,
            config=self.config,
        )
        reviewed = {}
        call_count = 0
        retry_count = 0
        debug_batches: list[dict[str, object]] = []
        review_method = getattr(
            self.dependencies.analyzer,
            "review_retention_candidates",
            None,
        )
        if not callable(review_method):
            raise AnalyzerProtocolError(
                "Retention review is enabled but the analyzer does not support it."
            )

        for batch in batches:
            batch_started_at = perf_counter()
            last_error = ""
            for attempt in range(self.config.analysis_batch_retry_limit + 1):
                result = None
                try:
                    call_count += 1
                    result = review_method(batch)
                    validated = validate_retention_review_result(
                        batch,
                        result,
                        policy,
                    )
                    reviewed.update(validated)
                    debug_batches.append(
                        _retention_review_debug_entry(
                            batch,
                            attempt=attempt,
                            status="success",
                            result=result,
                        )
                    )
                    log_timing(
                        logger,
                        "runner.stage.completed",
                        batch_started_at,
                        stage="retention_review",
                        batch_id=batch.batch_id,
                        candidate_count=len(batch.candidates),
                        retry_round=attempt,
                    )
                    break
                except NotImplementedError as exc:
                    last_error = "Retention review is not implemented by the analyzer."
                    debug_batches.append(
                        _retention_review_debug_entry(
                            batch,
                            attempt=attempt,
                            status="failed",
                            result=result,
                            error_summary=last_error,
                        )
                    )
                    if attempt >= self.config.analysis_batch_retry_limit:
                        self._dump_retention_review_debug_artifact(
                            target_date=target_date,
                            batches=debug_batches,
                            summary=RetentionReviewSummary(
                                selected_candidate_count=len(selected),
                                review_batch_count=len(batches),
                                review_retry_count=retry_count,
                            ),
                            error_summary=last_error,
                        )
                        raise AnalyzerProtocolError(last_error) from exc
                    retry_count += 1
                except AnalyzerProtocolError as exc:
                    last_error = str(exc)
                    debug_batches.append(
                        _retention_review_debug_entry(
                            batch,
                            attempt=attempt,
                            status="failed",
                            result=result,
                            error_summary=last_error,
                        )
                    )
                    if attempt >= self.config.analysis_batch_retry_limit:
                        self._dump_retention_review_debug_artifact(
                            target_date=target_date,
                            batches=debug_batches,
                            summary=RetentionReviewSummary(
                                selected_candidate_count=len(selected),
                                review_batch_count=len(batches),
                                review_retry_count=retry_count,
                            ),
                            error_summary=last_error,
                        )
                        raise AnalyzerProtocolError(
                            "Retention review failed after retries: " + last_error
                        ) from exc
                    retry_count += 1

        (
            kept,
            kept_reviewed_count,
            dropped_routine_count,
            dropped_uncertain_count,
        ) = apply_retention_review_results(candidates, reviewed, policy)
        summary = RetentionReviewSummary(
            selected_candidate_count=len(selected),
            reviewed_candidate_count=len(reviewed),
            kept_candidate_count=kept_reviewed_count,
            dropped_routine_count=dropped_routine_count,
            dropped_uncertain_count=dropped_uncertain_count,
            review_batch_count=len(batches),
            review_retry_count=retry_count,
        )
        self._dump_retention_review_debug_artifact(
            target_date=target_date,
            batches=debug_batches,
            summary=summary,
        )
        return kept, summary, call_count

    def _review_personal_event_facts(
        self,
        *,
        target_date: str,
        candidates: list[SourceBackedEventDraft],
        conversation_slices: list[ConversationSlice],
        messages: list[NormalizedMessage],
    ) -> tuple[list[SourceBackedEventDraft], PersonalFactReviewSummary, int]:
        policy = self.config.retention_policy
        if not policy.fact_review_enabled or not _supports_personal_fact_review(
            self.dependencies.analyzer
        ):
            return candidates, PersonalFactReviewSummary(), 0

        review_candidates = build_personal_fact_review_candidates(
            candidates,
            slices=conversation_slices,
            messages=messages,
            policy=policy,
        )
        if not review_candidates:
            return candidates, PersonalFactReviewSummary(), 0
        batches = pack_personal_fact_review_batches(
            target_date=target_date,
            candidates=review_candidates,
            config=self.config,
        )
        review_method = getattr(
            self.dependencies.analyzer,
            "review_personal_event_facts",
            None,
        )
        if not callable(review_method):
            raise AnalyzerProtocolError(
                "Personal fact review is enabled but the analyzer does not support it."
            )

        worker_count = min(
            len(batches),
            self.config.max_concurrent_personal_fact_review_requests,
        )
        review_started_at = perf_counter()
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(
                    self._review_personal_fact_batch_with_retry,
                    batch=batch,
                    review_method=review_method,
                )
                for batch in batches
            ]
            outcomes = [future.result() for future in futures]

        reviewed: dict[str, PersonalFactReviewItemResult] = {}
        debug_batches: list[dict[str, object]] = []
        call_count = sum(outcome.call_count for outcome in outcomes)
        retry_count = sum(outcome.retry_count for outcome in outcomes)
        log_timing(
            logger,
            "runner.stage.completed",
            review_started_at,
            stage="personal_fact_review_all",
            batch_count=len(batches),
            worker_count=worker_count,
            call_count=call_count,
            retry_count=retry_count,
        )
        for outcome in outcomes:
            reviewed.update(outcome.validated)
            debug_batches.extend(outcome.debug_entries)

        failed_outcome = next(
            (outcome for outcome in outcomes if outcome.error_summary),
            None,
        )
        if failed_outcome is not None:
            self._dump_personal_fact_review_debug_artifact(
                target_date=target_date,
                batches=debug_batches,
                summary=PersonalFactReviewSummary(
                    selected_candidate_count=len(review_candidates),
                    review_batch_count=len(batches),
                    review_retry_count=retry_count,
                ),
                error_summary=failed_outcome.error_summary,
            )
            if failed_outcome.error_kind == "not_implemented":
                raise AnalyzerProtocolError(failed_outcome.error_summary)
            raise AnalyzerProtocolError(
                "Personal fact review failed after retries: "
                + failed_outcome.error_summary
            )

        kept, confirmed_count, revised_count, dropped_count = (
            apply_personal_fact_review_results(
                candidates,
                review_candidates,
                reviewed,
                policy,
            )
        )
        summary = PersonalFactReviewSummary(
            selected_candidate_count=len(review_candidates),
            reviewed_candidate_count=len(reviewed),
            confirmed_candidate_count=confirmed_count,
            revised_candidate_count=revised_count,
            dropped_unsupported_count=dropped_count,
            review_batch_count=len(batches),
            review_retry_count=retry_count,
        )
        self._dump_personal_fact_review_debug_artifact(
            target_date=target_date,
            batches=debug_batches,
            summary=summary,
        )
        return kept, summary, call_count

    def _review_personal_fact_batch_with_retry(
        self,
        *,
        batch: PersonalFactReviewBatch,
        review_method,
    ) -> _PersonalFactReviewBatchOutcome:
        batch_started_at = perf_counter()
        last_error = ""
        debug_entries: list[dict[str, object]] = []
        retry_count = 0
        call_count = 0

        for attempt in range(self.config.analysis_batch_retry_limit + 1):
            result = None
            attempt_batch = (
                batch if not last_error else replace(batch, retry_feedback=last_error)
            )
            try:
                call_count += 1
                result = review_method(attempt_batch)
                validated = validate_personal_fact_review_result(attempt_batch, result)
                debug_entries.append(
                    _personal_fact_review_debug_entry(
                        batch,
                        attempt=attempt,
                        status="success",
                        result=result,
                    )
                )
                log_timing(
                    logger,
                    "runner.stage.completed",
                    batch_started_at,
                    stage="personal_fact_review",
                    batch_id=batch.batch_id,
                    candidate_count=len(batch.candidates),
                    retry_round=attempt,
                )
                return _PersonalFactReviewBatchOutcome(
                    batch=batch,
                    validated=validated,
                    debug_entries=debug_entries,
                    call_count=call_count,
                    retry_count=retry_count,
                )
            except NotImplementedError:
                last_error = "Personal fact review is not implemented by the analyzer."
                error_kind = "not_implemented"
            except AnalyzerProtocolError as exc:
                last_error = str(exc)
                error_kind = "protocol"

            debug_entries.append(
                _personal_fact_review_debug_entry(
                    batch,
                    attempt=attempt,
                    status="failed",
                    result=result,
                    error_summary=last_error,
                )
            )
            if attempt >= self.config.analysis_batch_retry_limit:
                return _PersonalFactReviewBatchOutcome(
                    batch=batch,
                    validated={},
                    debug_entries=debug_entries,
                    call_count=call_count,
                    retry_count=retry_count,
                    error_summary=last_error,
                    error_kind=error_kind,
                )
            retry_count += 1

        raise AssertionError("Personal fact review retry loop did not return.")

    def _dump_personal_fact_review_debug_artifact(
        self,
        *,
        target_date: str,
        batches: list[dict[str, object]],
        summary: PersonalFactReviewSummary,
        error_summary: str = "",
    ) -> None:
        debug_root = self.config.conversation_debug_root
        if debug_root is None:
            return
        date_dir = debug_root / target_date
        date_dir.mkdir(parents=True, exist_ok=True)
        (date_dir / "personal_fact_review.json").write_text(
            dump_json(
                {
                    "target_date": target_date,
                    "summary": summary.to_dict(),
                    "batches": batches,
                    "error_summary": error_summary,
                },
                pretty=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _dump_retention_review_debug_artifact(
        self,
        *,
        target_date: str,
        batches: list[dict[str, object]],
        summary: RetentionReviewSummary,
        error_summary: str = "",
    ) -> None:
        debug_root = self.config.conversation_debug_root
        if debug_root is None:
            return
        date_dir = debug_root / target_date
        date_dir.mkdir(parents=True, exist_ok=True)
        (date_dir / "retention_review.json").write_text(
            dump_json(
                {
                    "target_date": target_date,
                    "summary": summary.to_dict(),
                    "batches": batches,
                    "error_summary": error_summary,
                },
                pretty=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _write_empty_day(
        self,
        target_date: str,
        *,
        self_identity: SelfIdentity,
        conversation_count: int,
        message_count: int,
        slice_count: int,
        batch_count: int,
        warning_messages: list[str] | None = None,
        skipped_slice_count: int = 0,
        retention_review_summary: RetentionReviewSummary | None = None,
        personal_fact_review_summary: PersonalFactReviewSummary | None = None,
    ) -> DailyRunResult:
        warning_messages = warning_messages or []
        write_result = self.dependencies.event_store.replace_day(
            target_date,
            [],
            owner_display_name=self_identity.display_name,
        )
        if self.checkpoint_store is not None:
            self.checkpoint_store.clear()
        delivery_status, delivery_target, delivery_error = _deliver_markdown_to_self(
            self.dependencies.delivery_channel,
            self_identity=self_identity,
            markdown_path=Path(write_result.output_path),
        )
        if delivery_error:
            warning_messages.append(delivery_error)
        status = (
            DailyRunStatus.SUCCESS_WITH_WARNINGS.value
            if warning_messages or skipped_slice_count
            else DailyRunStatus.SUCCESS.value
        )
        return DailyRunResult(
            target_date=target_date,
            conversation_count=conversation_count,
            message_count=message_count,
            slice_count=slice_count,
            batch_count=batch_count,
            event_count=0,
            skipped_slice_count=skipped_slice_count,
            warning_count=len(warning_messages),
            status=status,
            output_path=write_result.output_path,
            error_summary="; ".join(warning_messages),
            self_delivery_status=delivery_status,
            self_delivery_target=delivery_target,
            self_delivery_error=delivery_error,
            retention_review_summary=(
                retention_review_summary or RetentionReviewSummary()
            ),
            personal_fact_review_summary=(
                personal_fact_review_summary or PersonalFactReviewSummary()
            ),
        )

    def _failed_result(self, target_date: str, error_summary: str) -> DailyRunResult:
        return DailyRunResult(
            target_date=target_date,
            conversation_count=0,
            message_count=0,
            slice_count=0,
            batch_count=0,
            event_count=0,
            skipped_slice_count=0,
            warning_count=0,
            status=DailyRunStatus.FAILED.value,
            output_path=None,
            error_summary=error_summary,
            self_delivery_status="",
            self_delivery_target="",
            self_delivery_error="",
            retention_review_summary=RetentionReviewSummary(),
            personal_fact_review_summary=PersonalFactReviewSummary(),
        )

    def _finish_run(
        self,
        run_started_at: float,
        result: DailyRunResult,
    ) -> DailyRunResult:
        self._dump_llm_usage_debug_artifact(target_date=result.target_date, status=result.status)
        log_timing(
            logger,
            "runner.run.completed",
            run_started_at,
            target_date=result.target_date,
            status=result.status,
            conversation_count=result.conversation_count,
            message_count=result.message_count,
            slice_count=result.slice_count,
            batch_count=result.batch_count,
            event_count=result.event_count,
            warning_count=result.warning_count,
            skipped_slice_count=result.skipped_slice_count,
            retention_review_selected=(
                result.retention_review_summary.selected_candidate_count
            ),
            retention_review_dropped=(
                result.retention_review_summary.dropped_routine_count
                + result.retention_review_summary.dropped_uncertain_count
            ),
            personal_fact_review_selected=(
                result.personal_fact_review_summary.selected_candidate_count
            ),
            personal_fact_review_revised=(
                result.personal_fact_review_summary.revised_candidate_count
            ),
        )
        return result

    def _dump_llm_usage_debug_artifact(self, *, target_date: str, status: str) -> None:
        debug_root = self.config.conversation_debug_root
        if debug_root is None:
            return
        date_dir = debug_root / target_date
        date_dir.mkdir(parents=True, exist_ok=True)
        (date_dir / "llm_usage.json").write_text(
            dump_json(
                {
                    "target_date": target_date,
                    "status": status,
                    "usage": self.dependencies.llm_usage_recorder.summary(),
                    "requests": self.dependencies.llm_usage_recorder.records(),
                },
                pretty=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _analyze_segmented_conversations(
        self,
        *,
        target_date: str,
        messages: list[NormalizedMessage],
        self_identity: SelfIdentity,
    ) -> tuple[
        list[SourceBackedEventDraft],
        list[ConversationSlice],
        list[str],
        int,
        int,
    ]:
        candidates: list[SourceBackedEventDraft] = []
        conversation_slices: list[ConversationSlice] = []
        warnings: list[str] = []
        skipped_segment_count = 0
        model_call_count = 0

        if self.config.use_initial_conversation_windows:
            anchor_units = build_initial_anchor_windows(
                messages,
                self_identity.open_id,
                max_anchor_gap_minutes=self.config.max_anchor_gap_minutes,
                max_unrelated_intervening_messages=self.config.max_unrelated_intervening_messages,
                initial_context_messages_before=self.config.initial_context_messages_before,
                reaction_catalog=self.reaction_catalog,
            )
            anchor_units = append_private_window_external_relations(
                anchor_units,
                chat_source=self.dependencies.chat_source,
                reaction_catalog=self.reaction_catalog,
            )
        else:
            anchor_units = group_anchor_units(
                messages,
                self_identity.open_id,
                before_limit=30,
                after_limit=30,
                reaction_catalog=self.reaction_catalog,
            )
        required_image_started_at = perf_counter()
        anchor_units = enrich_required_image_context(
            anchor_units,
            self_open_id=self_identity.open_id,
            chat_source=self.dependencies.chat_source,
            content_resolver=self.dependencies.content_resolver,
            reaction_catalog=self.reaction_catalog,
        )
        log_timing(
            logger,
            "runner.stage.completed",
            required_image_started_at,
            stage="load_required_image_context",
            anchor_count=len(anchor_units),
            image_summary_count=sum(
                len(unit.attachment_texts) for unit in anchor_units
            ),
        )
        anchors_by_conversation: dict[str, list] = {}
        for anchor_unit in anchor_units:
            anchors_by_conversation.setdefault(anchor_unit.conversation_id, []).append(
                anchor_unit
            )
        pending_conversation_analysis: list[
            tuple[list[ConversationSegmentUnit], bool, list[AnchorUnit]]
        ] = []

        segmentation_states: list[_ConversationSegmentationState] = []
        for conversation_id, conversation_anchors in sorted(
            anchors_by_conversation.items()
        ):
            if not conversation_anchors:
                continue
            hydrated_anchors = [
                self._hydrate_anchor_link_titles(item)
                for item in conversation_anchors
            ]
            segmentation_states.append(
                _ConversationSegmentationState(
                    conversation_id=conversation_id,
                    conversation_name=hydrated_anchors[0].conversation_name,
                    anchors=hydrated_anchors,
                )
            )

        ready_states = list(segmentation_states)
        running: dict[
            Future[_AnchorSegmentationOutcome],
            tuple[_ConversationSegmentationState, int, AnchorUnit],
        ] = {}

        def apply_outcome(
            state: _ConversationSegmentationState,
            anchor_index: int,
            anchor_unit: AnchorUnit,
            outcome: _AnchorSegmentationOutcome,
            *,
            cached: bool,
        ) -> None:
            nonlocal model_call_count
            if not cached:
                model_call_count += outcome.model_call_count
            if not outcome.units:
                state.fallback_required = True
                if not cached:
                    warnings.extend(outcome.warnings)
                    if outcome.error_summary:
                        warnings.append(
                            "Skipped anchor after segmentation retries failed: "
                            f"{outcome.error_summary}"
                        )
                    else:
                        warnings.append("Skipped anchor after invalid segmentation retries.")
                    state.failure_counts[outcome.failure_category] = (
                        state.failure_counts.get(outcome.failure_category, 0) + 1
                    )
                    if (
                        state.failure_counts[outcome.failure_category]
                        >= self.config.conversation_segmentation_failure_threshold
                    ):
                        state.circuit_open = True
                        state.skipped_anchor_count += (
                            len(state.anchors) - state.next_anchor_index
                        )
                        state.next_anchor_index = len(state.anchors)
                        warnings.append(
                            "Stopped remaining anchor segmentation after repeated "
                            f"{outcome.failure_category}."
                        )
                return

            state.units.extend(
                replace(
                    unit,
                    segment_id=f"anchor-{anchor_index:03d}:{unit.segment_id}",
                )
                for unit in outcome.units
                if set(unit.primary_message_ids) & set(anchor_unit.anchor_message_ids)
            )
            warnings.extend(outcome.warnings)
            log_timing(
                logger,
                "runner.stage.completed",
                outcome.started_at,
                stage="segment_conversation",
                conversation_id=state.conversation_id,
                segment_count=len(outcome.units),
                anchor_index=anchor_index,
                anchor_count=len(state.anchors),
                anchor_message_count=len(anchor_unit.anchor_message_ids),
                input_message_count=len(anchor_unit.messages),
                retry_round=outcome.retry_round,
            )

        with ThreadPoolExecutor(
            max_workers=self.config.max_concurrent_llm_requests
        ) as executor:
            while ready_states or running:
                while ready_states and len(running) < self.config.max_concurrent_llm_requests:
                    ready_states.sort(
                        key=lambda item: (
                            -_anchor_unit_input_size(
                                item.anchors[item.next_anchor_index], self.config
                            ),
                            item.conversation_id,
                        )
                    )
                    state = ready_states.pop(0)
                    if state.circuit_open or state.next_anchor_index >= len(state.anchors):
                        continue
                    anchor_index = state.next_anchor_index + 1
                    anchor_unit = state.anchors[state.next_anchor_index]
                    state.next_anchor_index += 1
                    signature = _anchor_unit_context_signature(anchor_unit)
                    cached = state.cache.get(signature)
                    if cached is not None:
                        apply_outcome(
                            state,
                            anchor_index,
                            anchor_unit,
                            cached,
                            cached=True,
                        )
                        if not state.circuit_open and state.next_anchor_index < len(state.anchors):
                            ready_states.append(state)
                        continue
                    future = executor.submit(
                        self._segment_anchor_window_with_retry,
                        target_date=target_date,
                        conversation_id=state.conversation_id,
                        conversation_name=state.conversation_name,
                        anchor_unit=anchor_unit,
                        self_identity=self_identity,
                    )
                    running[future] = (state, anchor_index, anchor_unit)

                if not running:
                    continue
                completed, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in completed:
                    state, anchor_index, anchor_unit = running.pop(future)
                    outcome = future.result()
                    state.cache[_anchor_unit_context_signature(anchor_unit)] = outcome
                    apply_outcome(
                        state,
                        anchor_index,
                        anchor_unit,
                        outcome,
                        cached=False,
                    )
                    if not state.circuit_open and state.next_anchor_index < len(state.anchors):
                        ready_states.append(state)

        for state in segmentation_states:
            if state.skipped_anchor_count:
                warnings.append(
                    "Skipped remaining anchor segmentation windows after circuit open: "
                    f"{state.skipped_anchor_count}."
                )
            pending_conversation_analysis.append(
                (
                    _dedupe_segment_primary_ownership(state.units),
                    state.fallback_required,
                    state.anchors,
                )
            )

        # Complete and persist every topic split before starting event extraction.
        analysis_jobs: list[tuple[SegmentAnalysisBatch, SelfIdentity]] = []
        fallback_jobs: list[list[AnchorUnit]] = []
        for conversation_units, fallback_required, conversation_anchors in pending_conversation_analysis:
            if conversation_units:
                conversation_slices.extend(
                    segment_unit_to_slice(unit) for unit in conversation_units
                )
                analysis_jobs.extend(
                    (batch, self_identity)
                    for batch in pack_segment_units(
                    target_date=target_date,
                    self_open_id=self_identity.open_id,
                    self_display_name=self_identity.display_name,
                    units=conversation_units,
                    config=self.config,
                    )
                )

            if fallback_required:
                warnings.append(
                    "Anchor segmentation retries were exhausted; running full-conversation anchor fallback."
                )
                fallback_jobs.append(conversation_anchors)

        analysis_jobs.sort(
            key=lambda item: _segment_batch_input_size(item[0], self.config),
            reverse=True,
        )
        event_extraction_workers = (
            self.config.max_concurrent_event_extraction_requests
            or self.config.max_concurrent_llm_requests
        )
        with ThreadPoolExecutor(max_workers=event_extraction_workers) as executor:
            futures = [
                executor.submit(
                    self._analyze_segment_batch_with_retry,
                    batch=batch,
                    self_identity=identity,
                )
                for batch, identity in analysis_jobs
            ]
            for future in futures:
                (
                    batch_candidates,
                    batch_warnings,
                    batch_skipped_count,
                    batch_call_count,
                ) = future.result()
                candidates.extend(batch_candidates)
                warnings.extend(batch_warnings)
                skipped_segment_count += batch_skipped_count
                model_call_count += batch_call_count

        for conversation_anchors in fallback_jobs:
            (
                fallback_candidates,
                fallback_warnings,
                fallback_skipped_count,
                fallback_call_count,
            ) = self._analyze_anchor_fallback(
                target_date=target_date,
                anchor_units=conversation_anchors,
                self_identity=self_identity,
            )
            candidates.extend(fallback_candidates)
            warnings.extend(fallback_warnings)
            skipped_segment_count += fallback_skipped_count
            model_call_count += fallback_call_count

        candidates, self_relation_warnings = filter_candidates_with_valid_self_relations(
            candidates
        )
        warnings.extend(self_relation_warnings)

        return (
            candidates,
            conversation_slices,
            warnings,
            skipped_segment_count,
            model_call_count,
        )

    def _segment_anchor_window_with_retry(
        self,
        *,
        target_date: str,
        conversation_id: str,
        conversation_name: str,
        anchor_unit: AnchorUnit,
        self_identity: SelfIdentity,
    ) -> _AnchorSegmentationOutcome:
        segmentation_messages = anchor_unit.messages
        response_signals = build_response_signals(
            segmentation_messages,
            self_open_id=self_identity.open_id,
            reaction_catalog=self.reaction_catalog,
        )
        hard_boundary_before_ids = build_hard_boundary_message_ids(
            segmentation_messages,
            self_open_id=self_identity.open_id,
        )
        started_at = perf_counter()
        checkpoint = (
            self.checkpoint_store.load_segmentation(anchor_unit)
            if self.checkpoint_store is not None
            else None
        )
        if checkpoint is not None:
            units, warnings = checkpoint
            units = _attach_anchor_attachment_texts(units, anchor_unit)
            return _AnchorSegmentationOutcome(
                units=units,
                warnings=warnings,
                error_summary="",
                retry_round=0,
                failure_category="",
                model_call_count=0,
                started_at=started_at,
            )

        units: list[ConversationSegmentUnit] = []
        segmentation_warnings: list[str] = []
        segmentation_error = ""
        model_call_count = 0
        retry_round = 0
        for retry_round in range(self.config.anchor_retry_limit + 1):
            started_at = perf_counter()
            segmentation_prompt = (
                self.dependencies.analyzer.build_segmentation_prompt(
                    target_date=target_date,
                    conversation_id=conversation_id,
                    conversation_name=conversation_name,
                    messages=segmentation_messages,
                    self_open_id=self_identity.open_id,
                    self_display_name=self_identity.display_name,
                    response_signals=response_signals,
                    hard_boundary_before_ids=hard_boundary_before_ids,
                    attachment_texts=anchor_unit.attachment_texts,
                )
                if hasattr(self.dependencies.analyzer, "build_segmentation_prompt")
                else None
            )
            try:
                segmentation_result = self.dependencies.analyzer.segment_conversation(
                    target_date=target_date,
                    conversation_id=conversation_id,
                    conversation_name=conversation_name,
                    messages=segmentation_messages,
                    self_open_id=self_identity.open_id,
                    self_display_name=self_identity.display_name,
                    response_signals=response_signals,
                    hard_boundary_before_ids=hard_boundary_before_ids,
                    attachment_texts=anchor_unit.attachment_texts,
                )
            except AnalyzerProtocolError as exc:
                model_call_count += 1
                segmentation_error = str(exc)
                self._dump_segmentation_failure_debug_artifacts(
                    target_date=target_date,
                    anchor_unit=anchor_unit,
                    retry_round=retry_round,
                    prompt=segmentation_prompt,
                    error_summary=segmentation_error,
                )
                continue

            model_call_count += 1
            segmentation_error = ""
            units, segmentation_warnings = validate_conversation_segmentation(
                segmentation_result,
                segmentation_messages,
                self_open_id=self_identity.open_id,
                self_display_name=self_identity.display_name,
                self_assignment_keywords=self.config.self_assignment_keywords,
                response_signals=response_signals,
            )
            units = _keep_relation_context_out_of_event_sources(
                units,
                relation_context_message_ids=set(
                    [
                        *anchor_unit.relation_context_message_ids,
                        *anchor_unit.timeline_context_message_ids,
                    ]
                ),
            )
            self._dump_segment_segmentation_debug_artifacts(
                target_date=target_date,
                anchor_unit=anchor_unit,
                retry_round=retry_round,
                prompt=segmentation_prompt,
                output_payload=segmentation_result.to_dict(),
                units=units,
                warnings=segmentation_warnings,
            )
            if units:
                units = _attach_anchor_attachment_texts(units, anchor_unit)
                if self.checkpoint_store is not None:
                    self.checkpoint_store.save_segmentation(
                        anchor_unit, units, segmentation_warnings
                    )
                break

        return _AnchorSegmentationOutcome(
            units=units,
            warnings=segmentation_warnings,
            error_summary=segmentation_error,
            retry_round=retry_round,
            failure_category=(
                ""
                if units
                else (
                    "analyzer_protocol_failure"
                    if segmentation_error
                    else "segmentation_validation_failure"
                )
            ),
            model_call_count=model_call_count,
            started_at=started_at,
        )


    def _hydrate_anchor_link_titles(self, anchor_unit: AnchorUnit) -> AnchorUnit:
        messages = [
            replace(message, links=list(self.dependencies.content_resolver.extract_links(message)))
            for message in anchor_unit.messages
        ]
        return replace(anchor_unit, messages=messages)

    def _analyze_anchor_fallback(
        self,
        *,
        target_date: str,
        anchor_units: list[AnchorUnit],
        self_identity: SelfIdentity,
    ) -> tuple[list[SourceBackedEventDraft], list[str], int, int]:
        if not hasattr(self.dependencies.analyzer, "analyze_anchor_batch"):
            return [], ["Anchor fallback is unavailable for this analyzer."], len(anchor_units), 0

        candidates: list[SourceBackedEventDraft] = []
        warnings: list[str] = []
        initial_results, initial_warnings, skipped_count, call_count = (
            self._analyze_anchor_units_resilient(
                target_date=target_date,
                anchor_units=anchor_units,
            )
        )
        warnings.extend(initial_warnings)
        pending = [
            (unit, initial_results[unit.anchor_unit_id], 0)
            for unit in anchor_units
            if unit.anchor_unit_id in initial_results
        ]

        while pending:
            expanded_groups: dict[
                tuple[str, ...], list[tuple[AnchorUnit, int]]
            ] = {}
            for unit, analysis, expansion_round in pending:
                slice_input = _anchor_unit_to_slice(unit)
                validated = validate_batch_analysis_result(
                    BatchAnalysisResult(
                        candidate_events=[
                            replace(
                                item,
                                source_conversation_id=slice_input.conversation_id,
                                source_slice_id=slice_input.slice_id,
                            )
                            for item in analysis.candidate_events
                        ],
                        context_requests=analysis.context_requests,
                    ),
                    {slice_input.slice_id: slice_input},
                    self_open_id=self_identity.open_id,
                    self_relation_keys=tuple(
                        item.key for item in self.config.self_relation_types
                    ),
                    fact_risk_keys=tuple(
                        item.key
                        for item in self.config.retention_policy.fact_risk_signals
                    ),
                    warning_sink=warnings,
                )
                if not validated.context_requests:
                    candidates.extend(validated.candidate_events)
                    continue
                if expansion_round >= self.config.anchor_retry_limit:
                    skipped_count += 1
                    warnings.append(
                        f"Anchor fallback still needs context after retries: {unit.anchor_unit_id}."
                    )
                    continue

                previous_signature = _anchor_unit_context_signature(unit)
                (
                    expanded_unit,
                    _,
                    attachment_texts,
                    _,
                    linked_file_texts,
                    _,
                ) = expand_anchor_unit_context(
                    unit,
                    validated.context_requests,
                    chat_source=self.dependencies.chat_source,
                    content_resolver=self.dependencies.content_resolver,
                    config=self.config,
                    reaction_catalog=self.reaction_catalog,
                    existing_attachment_texts=unit.attachment_texts,
                    existing_linked_file_texts=unit.linked_file_texts,
                )
                expanded_unit = replace(
                    expanded_unit,
                    attachment_texts=attachment_texts,
                    linked_file_texts=linked_file_texts,
                )
                if _anchor_unit_context_signature(expanded_unit) == previous_signature:
                    skipped_count += 1
                    warnings.append(
                        f"Anchor fallback expansion produced no new context: {unit.anchor_unit_id}."
                    )
                    continue
                request_signature = tuple(
                    sorted({item.request_type for item in validated.context_requests})
                )
                expanded_groups.setdefault(request_signature, []).append(
                    (expanded_unit, expansion_round + 1)
                )

            pending = []
            for expanded_items in expanded_groups.values():
                expanded_units = [item[0] for item in expanded_items]
                expansion_round_by_id = {
                    unit.anchor_unit_id: round_number
                    for unit, round_number in expanded_items
                }
                results, result_warnings, result_skipped, result_calls = (
                    self._analyze_anchor_units_resilient(
                        target_date=target_date,
                        anchor_units=expanded_units,
                    )
                )
                warnings.extend(result_warnings)
                skipped_count += result_skipped
                call_count += result_calls
                pending.extend(
                    (
                        unit,
                        results[unit.anchor_unit_id],
                        expansion_round_by_id[unit.anchor_unit_id],
                    )
                    for unit in expanded_units
                    if unit.anchor_unit_id in results
                )
        return candidates, warnings, skipped_count, call_count

    def _analyze_anchor_units_resilient(
        self,
        *,
        target_date: str,
        anchor_units: list[AnchorUnit],
    ) -> tuple[dict[str, AnchorAnalysisResult], list[str], int, int]:
        results: dict[str, AnchorAnalysisResult] = {}
        warnings: list[str] = []
        skipped_count = 0
        call_count = 0
        batch_size = max(self.config.anchor_batch_size, 1)
        for start in range(0, len(anchor_units), batch_size):
            (
                batch_results,
                batch_warnings,
                batch_skipped_count,
                batch_call_count,
            ) = self._resolve_anchor_batch(
                target_date=target_date,
                anchor_units=anchor_units[start : start + batch_size],
            )
            results.update(batch_results)
            warnings.extend(batch_warnings)
            skipped_count += batch_skipped_count
            call_count += batch_call_count
        return results, warnings, skipped_count, call_count

    def _resolve_anchor_batch(
        self,
        *,
        target_date: str,
        anchor_units: list[AnchorUnit],
    ) -> tuple[dict[str, AnchorAnalysisResult], list[str], int, int]:
        if not anchor_units:
            return {}, [], 0, 0

        warnings: list[str] = []
        call_count = 0
        final_valid: dict[str, AnchorAnalysisResult] = {}
        final_missing: list[AnchorUnit] = list(anchor_units)
        saw_response = False
        for attempt in range(self.config.anchor_batch_retry_limit + 1):
            prompt = build_anchor_batch_analysis_prompt(
                target_date,
                anchor_units,
                config=self.config,
            )
            try:
                result = self.dependencies.analyzer.analyze_anchor_batch(
                    target_date,
                    anchor_units,
                )
                call_count += 1
            except AnalyzerProtocolError as exc:
                call_count += 1
                self._dump_anchor_fallback_failure_debug_artifacts(
                    target_date=target_date,
                    anchor_units=anchor_units,
                    attempt=attempt,
                    prompt=prompt,
                    error_summary=str(exc),
                )
                continue

            saw_response = True
            final_valid, final_missing, invalid_count = _validate_anchor_batch_result(
                result.results,
                anchor_units,
            )
            validation_warnings = []
            if invalid_count:
                validation_warnings.append("Filtered invalid anchor fallback batch result.")
                warnings.extend(validation_warnings)
            self._dump_anchor_fallback_debug_artifacts(
                target_date=target_date,
                anchor_units=anchor_units,
                attempt=attempt,
                prompt=prompt,
                output_payload=result.to_dict(),
                valid_results=final_valid,
                missing_units=final_missing,
                warnings=validation_warnings,
            )
            if not final_missing:
                return final_valid, warnings, 0, call_count
            if attempt < self.config.anchor_batch_retry_limit:
                warnings.append("Anchor fallback batch was incomplete; retrying the same batch.")

        if saw_response and final_valid:
            (
                missing_results,
                missing_warnings,
                missing_skipped_count,
                missing_call_count,
            ) = self._resolve_anchor_batch(
                target_date=target_date,
                anchor_units=final_missing,
            )
            final_valid.update(missing_results)
            warnings.extend(missing_warnings)
            return (
                final_valid,
                warnings,
                missing_skipped_count,
                call_count + missing_call_count,
            )

        if len(anchor_units) > 1:
            midpoint = len(anchor_units) // 2
            left = self._resolve_anchor_batch(
                target_date=target_date,
                anchor_units=anchor_units[:midpoint],
            )
            right = self._resolve_anchor_batch(
                target_date=target_date,
                anchor_units=anchor_units[midpoint:],
            )
            return (
                left[0] | right[0],
                [*warnings, *left[1], *right[1]],
                left[2] + right[2],
                call_count + left[3] + right[3],
            )

        warnings.append("Skipped anchor fallback after repeated batch failures.")
        return {}, warnings, 1, call_count

    def _analyze_segment_batch_with_retry(
        self,
        *,
        batch: SegmentAnalysisBatch,
        self_identity: SelfIdentity,
        allow_context_expansion: bool = True,
        context_expansion_round: int = 0,
    ) -> tuple[list[SourceBackedEventDraft], list[str], int, int]:
        checkpoint = (
            self.checkpoint_store.load_analysis(batch)
            if self.checkpoint_store is not None
            else None
        )
        if checkpoint is not None:
            candidates, warnings, skipped_count = checkpoint
            return candidates, warnings, skipped_count, 0

        warnings: list[str] = []
        call_count = 0

        for attempt in range(self.config.analysis_batch_retry_limit + 1):
            try:
                batch_started_at = perf_counter()
                prompt = (
                    self.dependencies.analyzer.build_segment_batch_prompt(batch)
                    if hasattr(self.dependencies.analyzer, "build_segment_batch_prompt")
                    else None
                )
                result = self.dependencies.analyzer.analyze_segment_batch(batch)
                call_count += 1
                candidates, result_warnings, skipped_count, nested_call_count = (
                    self._collect_segment_batch_result(
                        result=result,
                        batch=batch,
                        self_identity=self_identity,
                        allow_context_expansion=allow_context_expansion,
                        context_expansion_round=context_expansion_round,
                    )
                )
                call_count += nested_call_count
                warnings.extend(result_warnings)
                self._dump_segment_batch_debug_artifacts(
                    batch=batch,
                    retry_round=attempt,
                    prompt=prompt,
                    output_payload=result.to_dict(),
                    candidates=candidates,
                    warnings=result_warnings,
                    skipped_count=skipped_count,
                )
                if self.checkpoint_store is not None:
                    self.checkpoint_store.save_analysis(
                        batch, candidates, warnings, skipped_count
                    )
                log_timing(
                    logger,
                    "runner.stage.completed",
                    batch_started_at,
                    stage="analyze_segment_batch",
                    conversation_id=batch.conversation_id,
                    segment_count=len(batch.segments),
                    retry_round=attempt,
                    candidate_event_count=len(candidates),
                )
                return candidates, warnings, skipped_count, call_count
            except AnalyzerProtocolError as exc:
                call_count += 1
                self._dump_segment_batch_failure_debug_artifacts(
                    batch=batch,
                    directory_name=f"analysis-{attempt + 1:02d}",
                    prompt=prompt,
                    stage="segment_batch",
                    attempt=attempt,
                    error_summary=str(exc),
                )
                if attempt < self.config.analysis_batch_retry_limit:
                    warnings.append("Segment batch failed; retrying the same batch.")
                    continue
                warnings.append("Segment batch failed repeatedly; retrying its segments separately.")
                batch_error = exc
                break
        else:
            return [], warnings, 0, call_count

        candidates: list[SourceBackedEventDraft] = []
        skipped_count = 0
        for unit in batch.segments:
            single_batch = SegmentAnalysisBatch(
                target_date=batch.target_date,
                conversation_id=batch.conversation_id,
                conversation_name=batch.conversation_name,
                self_open_id=batch.self_open_id,
                self_display_name=batch.self_display_name,
                segments=[unit],
            )
            prompt = (
                self.dependencies.analyzer.build_segment_batch_prompt(single_batch)
                if hasattr(self.dependencies.analyzer, "build_segment_batch_prompt")
                else None
            )
            try:
                result = self.dependencies.analyzer.analyze_segment_batch(single_batch)
                call_count += 1
            except AnalyzerProtocolError as exc:
                self._dump_segment_batch_failure_debug_artifacts(
                    batch=single_batch,
                    directory_name="fallback-01",
                    prompt=prompt,
                    stage="segment_fallback",
                    attempt=0,
                    error_summary=str(exc),
                )
                warnings.append(
                    f"Skipped segment after batch and single-segment analysis failures: {unit.segment_id}."
                )
                skipped_count += 1
                continue
            (
                unit_candidates,
                unit_warnings,
                unit_skipped_count,
                nested_call_count,
            ) = (
                self._collect_segment_batch_result(
                    result=result,
                    batch=single_batch,
                    self_identity=self_identity,
                    allow_context_expansion=allow_context_expansion,
                    context_expansion_round=context_expansion_round,
                )
            )
            call_count += nested_call_count
            candidates.extend(unit_candidates)
            warnings.extend(unit_warnings)
            skipped_count += unit_skipped_count
            self._dump_segment_batch_debug_artifacts(
                batch=single_batch,
                retry_round=0,
                prompt=prompt,
                output_payload=result.to_dict(),
                candidates=unit_candidates,
                warnings=unit_warnings,
                skipped_count=unit_skipped_count,
                directory_name="fallback-01",
            )

        logger.warning(
            "Segment batch fallback completed after analyzer failure: %s",
            batch_error,
        )
        return candidates, warnings, skipped_count, call_count

    def _collect_segment_batch_result(
        self,
        *,
        result: BatchSegmentAnalysisResult,
        batch: SegmentAnalysisBatch,
        self_identity: SelfIdentity,
        allow_context_expansion: bool,
        context_expansion_round: int,
    ) -> tuple[list[SourceBackedEventDraft], list[str], int, int]:
        analyses_by_segment, missing_units, warnings = validate_segment_batch_result(
            result,
            batch,
        )
        candidates: list[SourceBackedEventDraft] = []
        skipped_count = len(missing_units)
        nested_call_count = 0

        for unit in batch.segments:
            analysis = analyses_by_segment.get(unit.segment_id)
            if analysis is None:
                continue
            conversation_slice = segment_unit_to_slice(unit)
            analysis = replace(
                analysis,
                candidate_events=[
                    replace(
                        candidate,
                        source_conversation_id=conversation_slice.conversation_id,
                        source_slice_id=conversation_slice.slice_id,
                    )
                    for candidate in analysis.candidate_events
                ],
            )
            validated = validate_batch_analysis_result(
                analysis,
                {conversation_slice.slice_id: conversation_slice},
                self_open_id=self_identity.open_id,
                self_relation_keys=tuple(
                    item.key for item in self.config.self_relation_types
                ),
                fact_risk_keys=tuple(
                    item.key
                    for item in self.config.retention_policy.fact_risk_signals
                ),
                warning_sink=warnings,
            )
            if validated.context_requests:
                if (
                    not allow_context_expansion
                    or context_expansion_round >= self.config.context_expansion_round_limit
                ):
                    warnings.append(
                        f"Skipped segment that still needs additional context: {unit.segment_id}."
                    )
                    skipped_count += 1
                    continue
                (
                    retry_candidates,
                    retry_warnings,
                    retry_skipped_count,
                    retry_call_count,
                ) = self._retry_segment_context(
                    target_date=batch.target_date,
                    unit=unit,
                    requests=validated.context_requests,
                    self_identity=self_identity,
                    context_expansion_round=context_expansion_round + 1,
                )
                candidates.extend(retry_candidates)
                warnings.extend(retry_warnings)
                skipped_count += retry_skipped_count
                nested_call_count += retry_call_count
                continue
            candidates.extend(validated.candidate_events)

        return candidates, warnings, skipped_count, nested_call_count

    def _retry_segment_context(
        self,
        *,
        target_date: str,
        unit: ConversationSegmentUnit,
        requests: list[ContextRequest],
        self_identity: SelfIdentity,
        context_expansion_round: int,
    ) -> tuple[list[SourceBackedEventDraft], list[str], int, int]:
        warnings: list[str] = []
        base_slice = segment_unit_to_slice(unit)
        self._dump_segment_context_debug_artifacts(
            target_date=target_date,
            unit=unit,
            requests=requests,
            before=base_slice,
        )
        expanded_slice = expand_slice_context(
            base_slice,
            requests,
            chat_source=self.dependencies.chat_source,
            content_resolver=self.dependencies.content_resolver,
            config=self.config,
            reaction_catalog=self.reaction_catalog,
            warning_sink=warnings,
        )
        self._dump_segment_context_debug_artifacts(
            target_date=target_date,
            unit=unit,
            requests=requests,
            before=base_slice,
            after=expanded_slice,
        )
        if _conversation_slice_signature(expanded_slice) == _conversation_slice_signature(
            base_slice
        ):
            return (
                [],
                [f"Segment expansion produced no new context: {unit.segment_id}."],
                1,
                0,
            )

        expanded_by_id = {message.message_id: message for message in unit.messages}
        expanded_by_id.update(
            {message.message_id: message for message in expanded_slice.messages}
        )
        expanded_conversation_messages = sorted(
            expanded_by_id.values(),
            key=lambda item: (item.send_time, item.message_id),
        )
        response_signals = build_response_signals(
            expanded_conversation_messages,
            self_open_id=self_identity.open_id,
            reaction_catalog=self.reaction_catalog,
        )
        try:
            segmentation_result = self.dependencies.analyzer.segment_conversation(
                target_date=target_date,
                conversation_id=unit.conversation_id,
                conversation_name=unit.conversation_name,
                messages=expanded_conversation_messages,
                self_open_id=self_identity.open_id,
                self_display_name=self_identity.display_name,
                response_signals=response_signals,
                hard_boundary_before_ids=build_hard_boundary_message_ids(
                    expanded_conversation_messages,
                    self_open_id=self_identity.open_id,
                ),
            )
        except AnalyzerProtocolError:
            return (
                [],
                [f"Skipped segment because expanded-context segmentation failed: {unit.segment_id}."],
                1,
                1,
            )
        retry_units, segmentation_warnings = validate_conversation_segmentation(
            segmentation_result,
            expanded_conversation_messages,
            self_open_id=self_identity.open_id,
            self_display_name=self_identity.display_name,
            self_assignment_keywords=self.config.self_assignment_keywords,
            response_signals=response_signals,
        )
        warnings.extend(segmentation_warnings)
        original_primary_ids = set(unit.primary_message_ids)
        retry_units = [
            item
            for item in retry_units
            if original_primary_ids & set(item.primary_message_ids)
        ]
        if not retry_units:
            warnings.append(
                f"Skipped segment after expanded context changed its verified turn: {unit.segment_id}."
            )
            return [], warnings, 1, 1

        retry_units = [
            replace(
                item,
                attachment_texts=[
                    block
                    for block in expanded_slice.attachment_texts
                    if block.message_id in {message.message_id for message in item.messages}
                ],
                linked_file_texts=[
                    block
                    for block in expanded_slice.linked_file_texts
                    if block.message_id in {message.message_id for message in item.messages}
                ],
            )
            for item in retry_units
        ]
        candidates: list[SourceBackedEventDraft] = []
        skipped_count = 0
        call_count = 1
        for retry_batch in pack_segment_units(
            target_date=target_date,
            self_open_id=self_identity.open_id,
            self_display_name=self_identity.display_name,
            units=retry_units,
            config=self.config,
        ):
            (
                retry_candidates,
                retry_warnings,
                retry_skipped_count,
                retry_call_count,
            ) = self._analyze_segment_batch_with_retry(
                batch=retry_batch,
                self_identity=self_identity,
                allow_context_expansion=True,
                context_expansion_round=context_expansion_round,
            )
            candidates.extend(retry_candidates)
            warnings.extend(retry_warnings)
            skipped_count += retry_skipped_count
            call_count += retry_call_count
        return candidates, warnings, skipped_count, call_count

    def _analyze_conversation_slice_with_retry(
        self,
        *,
        target_date: str,
        conversation_slice: ConversationSlice,
        self_identity: SelfIdentity,
    ) -> tuple[BatchAnalysisResult, list[str], bool, int]:
        current_slice = conversation_slice
        warning_messages: list[str] = []
        run_count = 0

        for retry_round in range(0, self.config.slice_retry_limit + 1):
            if retry_round == 0:
                batch_input = AnalysisBatch(
                    target_date=target_date,
                    batch_id=f"conversation-{run_count + 1:03d}",
                    retry_round=0,
                    estimated_tokens=0,
                    self_open_id=self_identity.open_id,
                    self_display_name=self_identity.display_name,
                    slices=[current_slice],
                )
            else:
                batch_input = build_single_slice_retry_batch(
                    target_date,
                    current_slice,
                    retry_round=retry_round,
                    self_open_id=self_identity.open_id,
                    self_display_name=self_identity.display_name,
                    config=self.config,
                )

            batch_started_at = perf_counter()
            prompt = self.dependencies.analyzer.build_batch_prompt(
                batch_input
            ) if hasattr(self.dependencies.analyzer, "build_batch_prompt") else None
            try:
                batch_result = self.dependencies.analyzer.analyze_batch(target_date, batch_input)
            except AnalyzerProtocolError as exc:
                if retry_round == 0:
                    self._dump_conversation_debug_artifacts(
                        target_date=target_date,
                        conversation_slice=current_slice,
                        batch_input=batch_input,
                        prompt=prompt,
                        elapsed_ms=(perf_counter() - batch_started_at) * 1000,
                        error_summary=str(exc),
                    )
                raise
            validated_result = validate_batch_analysis_result(
                batch_result,
                {current_slice.slice_id: current_slice},
                self_open_id=self_identity.open_id,
                self_relation_keys=tuple(
                    item.key for item in self.config.self_relation_types
                ),
                fact_risk_keys=tuple(
                    item.key
                    for item in self.config.retention_policy.fact_risk_signals
                ),
                warning_sink=warning_messages,
            )
            run_count += 1
            if retry_round == 0:
                self._dump_conversation_debug_artifacts(
                    target_date=target_date,
                    conversation_slice=current_slice,
                    batch_input=batch_input,
                    prompt=prompt,
                    elapsed_ms=(perf_counter() - batch_started_at) * 1000,
                    output_payload=batch_result.to_dict(),
                    validated_result=validated_result,
                )
            log_timing(
                logger,
                "runner.stage.completed",
                batch_started_at,
                stage="analyze_conversation_slice",
                batch_id=batch_input.batch_id,
                slice_id=current_slice.slice_id,
                slice_count=1,
                retry_round=retry_round,
                candidate_event_count=len(validated_result.candidate_events),
                context_request_count=len(validated_result.context_requests),
            )

            if not validated_result.context_requests:
                return validated_result, warning_messages, False, run_count

            if retry_round >= self.config.slice_retry_limit:
                warning_messages.extend(
                    [
                        (
                            f"Slice needs more context after retries: {request.slice_id} "
                            f"({request.request_type}) {request.reason}"
                        )
                        for request in validated_result.context_requests
                    ]
                )
                return validated_result, warning_messages, True, run_count

            expanded_slice = expand_slice_context(
                current_slice,
                validated_result.context_requests,
                chat_source=self.dependencies.chat_source,
                content_resolver=self.dependencies.content_resolver,
                config=self.config,
                reaction_catalog=self.reaction_catalog,
            )
            if _conversation_slice_signature(expanded_slice) == _conversation_slice_signature(
                current_slice
            ):
                warning_messages.extend(
                    [
                        (
                            f"Slice expansion produced no new context: {request.slice_id} "
                            f"({request.request_type}) {request.reason}"
                        )
                        for request in validated_result.context_requests
                    ]
                )
                return validated_result, warning_messages, True, run_count

            current_slice = expanded_slice

        return BatchAnalysisResult(), warning_messages, True, run_count

    def _dump_conversation_debug_artifacts(
        self,
        *,
        target_date: str,
        conversation_slice: ConversationSlice,
        batch_input: AnalysisBatch,
        elapsed_ms: float,
        prompt: str | None,
        output_payload: dict[str, object] | None = None,
        validated_result: BatchAnalysisResult | None = None,
        error_summary: str | None = None,
    ) -> None:
        debug_root = self.config.conversation_debug_root
        if debug_root is None:
            return

        conversation_dir = (
            debug_root
            / target_date
            / _safe_conversation_dir_name(conversation_slice.slice_id)
            / "pass_01"
        )
        conversation_dir.mkdir(parents=True, exist_ok=True)
        (conversation_dir / "input.json").write_text(
            dump_json(batch_input.to_dict(), pretty=True) + "\n",
            encoding="utf-8",
        )
        if prompt is not None:
            (conversation_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        if output_payload is not None:
            (conversation_dir / "output.json").write_text(
                dump_json(output_payload, pretty=True) + "\n",
                encoding="utf-8",
            )

        meta = {
            "target_date": target_date,
            "slice_id": conversation_slice.slice_id,
            "conversation_id": conversation_slice.conversation_id,
            "conversation_name": conversation_slice.conversation_name,
            "retry_round": batch_input.retry_round,
            "elapsed_ms": round(elapsed_ms, 3),
            "message_count": len(conversation_slice.messages),
            "anchor_message_count": len(conversation_slice.anchor_message_ids),
            "in_day_message_count": len(conversation_slice.in_day_message_ids),
            "candidate_event_count": (
                len(validated_result.candidate_events) if validated_result is not None else None
            ),
            "context_request_count": (
                len(validated_result.context_requests) if validated_result is not None else None
            ),
            "status": "failed" if error_summary else "completed",
            "error_summary": error_summary or "",
        }
        (conversation_dir / "meta.json").write_text(
            dump_json(meta, pretty=True) + "\n",
            encoding="utf-8",
        )

    def _dump_segment_segmentation_debug_artifacts(
        self,
        *,
        target_date: str,
        anchor_unit: AnchorUnit,
        retry_round: int,
        prompt: str | None,
        output_payload: dict[str, object],
        units: list[ConversationSegmentUnit],
        warnings: list[str],
    ) -> None:
        debug_root = self.config.conversation_debug_root
        if debug_root is None:
            return
        directory = (
            debug_root
            / target_date
            / "_segment_batches"
            / _safe_conversation_dir_name(anchor_unit.conversation_id)
            / _safe_conversation_dir_name(anchor_unit.anchor_unit_id)
            / f"segmentation-{retry_round + 1:02d}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "segmentation_input.json").write_text(
            dump_json(anchor_unit.to_dict(), pretty=True) + "\n",
            encoding="utf-8",
        )
        if prompt is not None:
            (directory / "segmentation_prompt.txt").write_text(prompt, encoding="utf-8")
        (directory / "segmentation_output.json").write_text(
            dump_json(output_payload, pretty=True) + "\n",
            encoding="utf-8",
        )
        (directory / "segmentation_validation.json").write_text(
            dump_json(
                {
                    "units": [item.to_dict() for item in units],
                    "warnings": list(warnings),
                },
                pretty=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _dump_segmentation_failure_debug_artifacts(
        self,
        *,
        target_date: str,
        anchor_unit: AnchorUnit,
        retry_round: int,
        prompt: str | None,
        error_summary: str,
    ) -> None:
        debug_root = self.config.conversation_debug_root
        if debug_root is None:
            return
        directory = (
            debug_root
            / target_date
            / "_segment_batches"
            / _safe_conversation_dir_name(anchor_unit.conversation_id)
            / _safe_conversation_dir_name(anchor_unit.anchor_unit_id)
            / f"segmentation-{retry_round + 1:02d}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "segmentation_input.json").write_text(
            dump_json(anchor_unit.to_dict(), pretty=True) + "\n",
            encoding="utf-8",
        )
        if prompt is not None:
            (directory / "segmentation_prompt.txt").write_text(prompt, encoding="utf-8")
        (directory / "failure.json").write_text(
            dump_json(
                {
                    "stage": "segmentation",
                    "status": "failed",
                    "attempt": retry_round + 1,
                    "anchor_unit_id": anchor_unit.anchor_unit_id,
                    "anchor_message_ids": list(anchor_unit.anchor_message_ids),
                    "error_summary": error_summary,
                },
                pretty=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _dump_segment_batch_debug_artifacts(
        self,
        *,
        batch: SegmentAnalysisBatch,
        retry_round: int,
        prompt: str | None,
        output_payload: dict[str, object],
        candidates: list[SourceBackedEventDraft],
        warnings: list[str],
        skipped_count: int,
        directory_name: str | None = None,
    ) -> None:
        debug_root = self.config.conversation_debug_root
        if debug_root is None:
            return
        segment_key = "-".join(item.segment_id for item in batch.segments)
        directory = (
            debug_root
            / batch.target_date
            / "_segment_batches"
            / _safe_conversation_dir_name(batch.conversation_id)
            / _safe_segment_batch_dir_name(segment_key)
            / (directory_name or f"analysis-{retry_round + 1:02d}")
        )
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "input.json").write_text(
            dump_json(batch.to_dict(), pretty=True) + "\n",
            encoding="utf-8",
        )
        if prompt is not None:
            (directory / "prompt.txt").write_text(prompt, encoding="utf-8")
        (directory / "output.json").write_text(
            dump_json(output_payload, pretty=True) + "\n",
            encoding="utf-8",
        )
        (directory / "candidate_validation.json").write_text(
            dump_json(
                {
                    "retained_candidates": [item.to_dict() for item in candidates],
                    "skipped_count": skipped_count,
                    "warnings": list(warnings),
                },
                pretty=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _dump_segment_batch_failure_debug_artifacts(
        self,
        *,
        batch: SegmentAnalysisBatch,
        directory_name: str,
        prompt: str | None,
        stage: str,
        attempt: int,
        error_summary: str,
    ) -> None:
        debug_root = self.config.conversation_debug_root
        if debug_root is None:
            return
        segment_key = "-".join(item.segment_id for item in batch.segments)
        directory = (
            debug_root
            / batch.target_date
            / "_segment_batches"
            / _safe_conversation_dir_name(batch.conversation_id)
            / _safe_segment_batch_dir_name(segment_key)
            / directory_name
        )
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "input.json").write_text(
            dump_json(batch.to_dict(), pretty=True) + "\n",
            encoding="utf-8",
        )
        if prompt is not None:
            (directory / "prompt.txt").write_text(prompt, encoding="utf-8")
        (directory / "failure.json").write_text(
            dump_json(
                {
                    "stage": stage,
                    "status": "failed",
                    "attempt": attempt + 1,
                    "segment_ids": [item.segment_id for item in batch.segments],
                    "error_summary": error_summary,
                },
                pretty=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _dump_anchor_fallback_debug_artifacts(
        self,
        *,
        target_date: str,
        anchor_units: list[AnchorUnit],
        attempt: int,
        prompt: str,
        output_payload: dict[str, object],
        valid_results: dict[str, AnchorAnalysisResult],
        missing_units: list[AnchorUnit],
        warnings: list[str],
    ) -> None:
        directory = self._anchor_fallback_debug_directory(
            target_date=target_date,
            anchor_units=anchor_units,
            attempt=attempt,
        )
        if directory is None:
            return
        self._write_anchor_fallback_debug_input(
            directory=directory,
            target_date=target_date,
            anchor_units=anchor_units,
            prompt=prompt,
        )
        (directory / "output.json").write_text(
            dump_json(output_payload, pretty=True) + "\n",
            encoding="utf-8",
        )
        (directory / "validation.json").write_text(
            dump_json(
                {
                    "valid_results": {
                        anchor_unit_id: result.to_dict()
                        for anchor_unit_id, result in valid_results.items()
                    },
                    "missing_anchor_unit_ids": [
                        item.anchor_unit_id for item in missing_units
                    ],
                    "warnings": list(warnings),
                },
                pretty=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _dump_anchor_fallback_failure_debug_artifacts(
        self,
        *,
        target_date: str,
        anchor_units: list[AnchorUnit],
        attempt: int,
        prompt: str,
        error_summary: str,
    ) -> None:
        directory = self._anchor_fallback_debug_directory(
            target_date=target_date,
            anchor_units=anchor_units,
            attempt=attempt,
        )
        if directory is None:
            return
        self._write_anchor_fallback_debug_input(
            directory=directory,
            target_date=target_date,
            anchor_units=anchor_units,
            prompt=prompt,
        )
        (directory / "failure.json").write_text(
            dump_json(
                {
                    "stage": "anchor_fallback",
                    "status": "failed",
                    "attempt": attempt + 1,
                    "anchor_unit_ids": [item.anchor_unit_id for item in anchor_units],
                    "error_summary": error_summary,
                },
                pretty=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _anchor_fallback_debug_directory(
        self,
        *,
        target_date: str,
        anchor_units: list[AnchorUnit],
        attempt: int,
    ) -> Path | None:
        debug_root = self.config.conversation_debug_root
        if debug_root is None or not anchor_units:
            return None
        serialized_units = dump_json(
            [item.to_dict() for item in anchor_units],
            pretty=False,
        )
        input_fingerprint = sha1(serialized_units.encode("utf-8")).hexdigest()[:12]
        anchor_key = f"{anchor_units[0].anchor_unit_id}-{input_fingerprint}"
        directory = (
            debug_root
            / target_date
            / "_anchor_fallback"
            / _safe_conversation_dir_name(anchor_units[0].conversation_id)
            / _safe_conversation_dir_name(anchor_key)
            / f"attempt-{attempt + 1:02d}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _write_anchor_fallback_debug_input(
        self,
        *,
        directory: Path,
        target_date: str,
        anchor_units: list[AnchorUnit],
        prompt: str,
    ) -> None:
        (directory / "input.json").write_text(
            dump_json(
                {
                    "target_date": target_date,
                    "anchor_units": [item.to_dict() for item in anchor_units],
                },
                pretty=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (directory / "prompt.txt").write_text(prompt, encoding="utf-8")

    def _dump_segment_context_debug_artifacts(
        self,
        *,
        target_date: str,
        unit: ConversationSegmentUnit,
        requests: list[ContextRequest],
        before: ConversationSlice,
        after: ConversationSlice | None = None,
    ) -> None:
        debug_root = self.config.conversation_debug_root
        if debug_root is None:
            return
        directory = (
            debug_root
            / target_date
            / "_segment_batches"
            / _safe_conversation_dir_name(unit.conversation_id)
            / _safe_conversation_dir_name(unit.segment_id)
            / "context_expansion"
        )
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "requests.json").write_text(
            dump_json([item.to_dict() for item in requests], pretty=True) + "\n",
            encoding="utf-8",
        )
        (directory / "before.json").write_text(
            dump_json(before.to_dict(), pretty=True) + "\n",
            encoding="utf-8",
        )
        if after is not None:
            (directory / "after.json").write_text(
                dump_json(after.to_dict(), pretty=True) + "\n",
                encoding="utf-8",
            )

    def _resolve_workstream_groups(
        self,
        *,
        target_date: str,
        model_groups: list[CrossConversationGroup],
        candidates: list[SourceBackedEventDraft],
    ) -> tuple[list[CrossConversationGroup], list[str]]:
        request_json = getattr(self.dependencies.analyzer, "request_json", None)
        if not callable(request_json):
            return consolidate_workstream_groups(model_groups, candidates)

        prompt = build_workstream_assignment_prompt(target_date, candidates)
        try:
            payload = request_json(
                prompt,
                output_schema=workstream_assignment_output_schema(),
            )
            if not isinstance(payload, dict):
                raise TypeError("Workstream assignment response must be an object.")
            assignment_result = WorkstreamAssignmentResult.from_dict(payload)
            groups, warnings = groups_from_workstream_assignments(
                assignment_result,
                candidates,
            )
            assignment_result, followup_warnings = self._resolve_unassigned_workstreams(
                target_date=target_date,
                request_json=request_json,
                initial_result=assignment_result,
                initial_groups=groups,
                candidates=candidates,
            )
            groups, assignment_warnings = groups_from_workstream_assignments(
                assignment_result,
                candidates,
            )
            warnings.extend([*followup_warnings, *assignment_warnings])
        except (AnalyzerProtocolError, TypeError, ValueError) as exc:
            fallback_groups, fallback_warnings = consolidate_workstream_groups(
                model_groups,
                candidates,
            )
            warnings = [
                f"Skipped LLM workstream resolution: {exc}",
                *fallback_warnings,
            ]
            self._dump_workstream_resolution_debug_artifacts(
                target_date=target_date,
                prompt=prompt,
                candidates=candidates,
                output_payload=None,
                groups=fallback_groups,
                warnings=warnings,
            )
            return fallback_groups, warnings

        self._dump_workstream_resolution_debug_artifacts(
            target_date=target_date,
            prompt=prompt,
            candidates=candidates,
            output_payload=assignment_result.to_dict(),
            groups=groups,
            warnings=warnings,
        )
        return groups, warnings

    def _resolve_unassigned_workstreams(
        self,
        *,
        target_date: str,
        request_json,
        initial_result: WorkstreamAssignmentResult,
        initial_groups: list[CrossConversationGroup],
        candidates: list[SourceBackedEventDraft],
    ) -> tuple[WorkstreamAssignmentResult, list[str]]:
        assignments_by_id = {
            assignment.draft_id: assignment
            for assignment in initial_result.assignments
        }
        unassigned_candidates = [
            candidate
            for candidate in candidates
            if not assignments_by_id.get(candidate.draft_id, WorkstreamAssignment("", "")).parent_draft_id
        ]
        known_workstreams = _build_known_workstream_context(
            initial_result,
            initial_groups,
            candidates,
        )
        if not unassigned_candidates or not known_workstreams:
            return initial_result, []

        prompt = build_unassigned_workstream_assignment_prompt(
            target_date,
            known_workstreams=known_workstreams,
            unassigned_candidates=unassigned_candidates,
        )
        try:
            payload = request_json(
                prompt,
                output_schema=workstream_assignment_output_schema(),
            )
            if not isinstance(payload, dict):
                raise TypeError("Unassigned workstream response must be an object.")
            followup_result = WorkstreamAssignmentResult.from_dict(payload)
        except (AnalyzerProtocolError, TypeError, ValueError) as exc:
            self._dump_workstream_followup_debug_artifacts(
                target_date=target_date,
                known_workstreams=known_workstreams,
                unassigned_candidates=unassigned_candidates,
                prompt=prompt,
                output_payload=None,
                warnings=[f"Skipped LLM unassigned workstream review: {exc}"],
            )
            return initial_result, [f"Skipped LLM unassigned workstream review: {exc}"]

        unassigned_ids = {candidate.draft_id for candidate in unassigned_candidates}
        followup_by_id: dict[str, WorkstreamAssignment] = {}
        warnings: list[str] = []
        for assignment in followup_result.assignments:
            if assignment.draft_id not in unassigned_ids:
                warnings.append(
                    f"Ignored follow-up assignment outside unresolved candidates: {assignment.draft_id}."
                )
                continue
            if assignment.draft_id in followup_by_id:
                warnings.append(
                    f"Ignored duplicate follow-up assignment: {assignment.draft_id}."
                )
                continue
            if assignment.root_workstream_name.strip():
                warnings.append(
                    f"Ignored follow-up attempt to create a workstream root: {assignment.draft_id}."
                )
                continue
            followup_by_id[assignment.draft_id] = assignment

        merged_result = WorkstreamAssignmentResult(
            assignments=[
                followup_by_id.get(candidate.draft_id, assignments_by_id.get(candidate.draft_id, WorkstreamAssignment(candidate.draft_id, "")))
                for candidate in candidates
            ]
        )
        self._dump_workstream_followup_debug_artifacts(
            target_date=target_date,
            known_workstreams=known_workstreams,
            unassigned_candidates=unassigned_candidates,
            prompt=prompt,
            output_payload=followup_result.to_dict(),
            warnings=warnings,
        )
        return merged_result, warnings

    def _dump_merge_debug_artifacts(
        self,
        *,
        target_date: str,
        candidates: list[SourceBackedEventDraft],
        output_payload: dict[str, object] | None = None,
    ) -> None:
        debug_root = self.config.conversation_debug_root
        if debug_root is None:
            return

        merge_dir = debug_root / target_date / "_merge_day_candidates"
        merge_dir.mkdir(parents=True, exist_ok=True)
        input_payload = {
            "target_date": target_date,
            "candidates": [candidate.to_dict() for candidate in candidates],
        }
        (merge_dir / "input.json").write_text(
            dump_json(input_payload, pretty=True) + "\n",
            encoding="utf-8",
        )
        if hasattr(self.dependencies.analyzer, "build_merge_prompt"):
            prompt = self.dependencies.analyzer.build_merge_prompt(target_date, candidates)
            (merge_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        if output_payload is not None:
            (merge_dir / "output.json").write_text(
                dump_json(output_payload, pretty=True) + "\n",
                encoding="utf-8",
            )

    def _dump_workstream_followup_debug_artifacts(
        self,
        *,
        target_date: str,
        known_workstreams: list[dict[str, object]],
        unassigned_candidates: list[SourceBackedEventDraft],
        prompt: str,
        output_payload: dict[str, object] | None,
        warnings: list[str],
    ) -> None:
        debug_root = self.config.conversation_debug_root
        if debug_root is None:
            return
        merge_dir = debug_root / target_date / "_merge_day_candidates"
        merge_dir.mkdir(parents=True, exist_ok=True)
        (merge_dir / "workstream_resolution_followup_input.json").write_text(
            dump_json(
                {
                    "known_workstreams": known_workstreams,
                    "unassigned_candidates": [
                        candidate.to_dict() for candidate in unassigned_candidates
                    ],
                },
                pretty=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (merge_dir / "workstream_resolution_followup_prompt.txt").write_text(
            prompt,
            encoding="utf-8",
        )
        if output_payload is not None:
            (merge_dir / "workstream_resolution_followup_output.json").write_text(
                dump_json(output_payload, pretty=True) + "\n",
                encoding="utf-8",
            )
        (merge_dir / "workstream_resolution_followup_validation.json").write_text(
            dump_json({"warnings": list(warnings)}, pretty=True) + "\n",
            encoding="utf-8",
        )

    def _dump_workstream_resolution_debug_artifacts(
        self,
        *,
        target_date: str,
        prompt: str,
        candidates: list[SourceBackedEventDraft],
        output_payload: dict[str, object] | None,
        groups: list[CrossConversationGroup],
        warnings: list[str],
    ) -> None:
        debug_root = self.config.conversation_debug_root
        if debug_root is None:
            return
        merge_dir = debug_root / target_date / "_merge_day_candidates"
        merge_dir.mkdir(parents=True, exist_ok=True)
        (merge_dir / "workstream_resolution_input.json").write_text(
            dump_json(
                {
                    "candidates": [candidate.to_dict() for candidate in candidates],
                },
                pretty=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (merge_dir / "workstream_resolution_prompt.txt").write_text(
            prompt,
            encoding="utf-8",
        )
        if output_payload is not None:
            (merge_dir / "workstream_resolution_output.json").write_text(
                dump_json(output_payload, pretty=True) + "\n",
                encoding="utf-8",
            )
        (merge_dir / "workstream_resolution_validated.json").write_text(
            dump_json(
                {
                    "groups": [group.to_dict() for group in groups],
                    "warnings": list(warnings),
                },
                pretty=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _dump_resolved_merge_groups(
        self,
        *,
        target_date: str,
        groups: list[CrossConversationGroup],
        warnings: list[str],
    ) -> None:
        debug_root = self.config.conversation_debug_root
        if debug_root is None:
            return
        merge_dir = debug_root / target_date / "_merge_day_candidates"
        merge_dir.mkdir(parents=True, exist_ok=True)
        (merge_dir / "resolved_groups.json").write_text(
            dump_json(
                {
                    "groups": [group.to_dict() for group in groups],
                    "warnings": list(warnings),
                },
                pretty=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _dump_final_events_debug_artifacts(
        self,
        *,
        target_date: str,
        merged_drafts: list[MergedEventDraft],
        events: list[WorkEvent],
        event_build_warnings: list[str],
        final_filter_warnings: list[str],
        retention_warnings: list[str],
    ) -> None:
        debug_root = self.config.conversation_debug_root
        if debug_root is None:
            return
        date_dir = debug_root / target_date
        date_dir.mkdir(parents=True, exist_ok=True)
        (date_dir / "final_events.json").write_text(
            dump_json(
                {
                    "target_date": target_date,
                    "merged_drafts": [draft.to_dict() for draft in merged_drafts],
                    "events": [event.to_dict() for event in events],
                    "warnings": {
                        "event_build": list(event_build_warnings),
                        "final_filter": list(final_filter_warnings),
                        "retention": list(retention_warnings),
                    },
                },
                pretty=True,
            )
            + "\n",
            encoding="utf-8",
        )


def _supports_segment_batches(analyzer: object) -> bool:
    return all(
        callable(getattr(analyzer, method_name, None))
        for method_name in ("segment_conversation", "analyze_segment_batch")
    )


def _retention_review_debug_entry(
    batch: RetentionReviewBatch,
    *,
    attempt: int,
    status: str,
    result: RetentionReviewResult | None,
    error_summary: str = "",
) -> dict[str, object]:
    return {
        "batch_id": batch.batch_id,
        "attempt": attempt,
        "status": status,
        "candidates": [
            {
                "draft_id": item.candidate.draft_id,
                "before": _source_backed_event_debug_summary(item.candidate),
                "source_message_ids": list(item.candidate.source_message_ids),
                "allowed_evidence_message_ids": list(
                    item.allowed_evidence_message_ids
                ),
            }
            for item in batch.candidates
        ],
        "result": result.to_dict() if result is not None else None,
        "coverage": _retention_review_coverage(result),
        "error_summary": error_summary,
    }


def _personal_fact_review_debug_entry(
    batch: PersonalFactReviewBatch,
    *,
    attempt: int,
    status: str,
    result: PersonalFactReviewResult | None,
    error_summary: str = "",
) -> dict[str, object]:
    return {
        "batch_id": batch.batch_id,
        "attempt": attempt,
        "status": status,
        "candidates": [
            {
                "draft_id": item.candidate.draft_id,
                "review_reasons": list(item.review_reasons),
                "before": _source_backed_event_debug_summary(item.candidate),
                "source_message_ids": list(item.candidate.source_message_ids),
                "allowed_evidence_message_ids": list(
                    item.allowed_evidence_message_ids
                ),
            }
            for item in batch.candidates
        ],
        "result": result.to_dict() if result is not None else None,
        "coverage": _personal_fact_review_coverage(result),
        "error_summary": error_summary,
    }


def _source_backed_event_debug_summary(
    candidate: SourceBackedEventDraft,
) -> dict[str, object]:
    return {
        "topic": candidate.topic,
        "content": candidate.content,
        "action_label": candidate.action_label,
        "object_hint": candidate.object_hint,
        "retention_reason": candidate.retention_reason,
        "retention_detail": candidate.retention_detail,
        "workstream_key": candidate.workstream_key,
        "fact_items": [item.to_dict() for item in candidate.fact_items],
        "fact_risk_flags": list(candidate.fact_risk_flags),
    }


def _retention_review_coverage(
    result: RetentionReviewResult | None,
) -> dict[str, object]:
    if result is None:
        return {}
    coverage: dict[str, object] = {}
    for item in result.results:
        routine_evidence = [
            message_id
            for signal in item.routine_signals
            for message_id in signal.evidence_message_ids
        ]
        substantive_evidence = [
            message_id
            for signal in item.substantive_signals
            for message_id in signal.evidence_message_ids
        ]
        coverage[item.draft_id] = {
            "routine_signal_count": len(item.routine_signals),
            "substantive_signal_count": len(item.substantive_signals),
            "routine_evidence_message_ids": list(dict.fromkeys(routine_evidence)),
            "substantive_evidence_message_ids": list(
                dict.fromkeys(substantive_evidence)
            ),
        }
    return coverage


def _personal_fact_review_coverage(
    result: PersonalFactReviewResult | None,
) -> dict[str, object]:
    if result is None:
        return {}
    coverage: dict[str, object] = {}
    for item in result.results:
        evidence_ids = [
            message_id
            for fact in item.fact_items
            for message_id in fact.evidence_message_ids
        ]
        coverage[item.draft_id] = {
            "supported": item.supported,
            "fact_item_count": len(item.fact_items),
            "covered_fields": list(
                dict.fromkeys(fact.field_name for fact in item.fact_items)
            ),
            "evidence_message_ids": list(dict.fromkeys(evidence_ids)),
            "removed_claim_count": len(item.removed_claims),
        }
    return coverage


def _supports_personal_fact_review(analyzer: object) -> bool:
    method = getattr(analyzer, "review_personal_event_facts", None)
    implementation = getattr(method, "__func__", method)
    return bool(
        callable(method)
        and implementation is not Analyzer.review_personal_event_facts
    )


def _build_known_workstream_context(
    result: WorkstreamAssignmentResult,
    groups: list[CrossConversationGroup],
    candidates: list[SourceBackedEventDraft],
) -> list[dict[str, object]]:
    candidate_by_id = {candidate.draft_id: candidate for candidate in candidates}
    root_names = {
        assignment.draft_id: assignment.root_workstream_name
        for assignment in result.assignments
        if (
            assignment.draft_id == assignment.parent_draft_id
            and assignment.root_workstream_name.strip()
        )
    }
    context: list[dict[str, object]] = []
    for group in groups:
        root_name = root_names.get(group.primary_draft_id, "")
        if not root_name:
            continue
        members = [
            candidate_by_id[draft_id]
            for draft_id in group.draft_ids
            if draft_id in candidate_by_id
        ]
        context.append(
            {
                "root_draft_id": group.primary_draft_id,
                "root_workstream_name": root_name,
                "members": [
                    {
                        "draft_id": member.draft_id,
                        "topic": member.topic,
                        "content": member.content,
                        "object_hint": member.object_hint,
                        "source_message_ids": member.source_message_ids,
                    }
                    for member in members
                ],
            }
        )
    return context


def _anchor_unit_to_slice(anchor_unit: AnchorUnit) -> ConversationSlice:
    main_ids = list(anchor_unit.base_message_ids)
    relation_ids = list(
        dict.fromkeys(
            [
                *anchor_unit.relation_context_message_ids,
                *anchor_unit.timeline_context_message_ids,
            ]
        )
    )
    return ConversationSlice(
        slice_id=f"{anchor_unit.conversation_id}:anchor:{anchor_unit.anchor_unit_id}",
        conversation_id=anchor_unit.conversation_id,
        conversation_name=anchor_unit.conversation_name,
        anchor_message_ids=list(anchor_unit.anchor_message_ids),
        in_day_message_ids=main_ids,
        messages=list(anchor_unit.messages),
        attachment_texts=list(anchor_unit.attachment_texts),
        linked_file_texts=list(anchor_unit.linked_file_texts),
        primary_message_ids=main_ids,
        context_message_ids=relation_ids,
        self_evidence_message_ids=[
            message_id
            for message_id in anchor_unit.anchor_message_ids
            if message_id in set(main_ids)
        ],
    )


def _message_input_size(message: NormalizedMessage, config: RuntimeConfig) -> int:
    return (
        min(len(message.text), config.prompt_message_char_limit)
        + sum(
            len(item.file_name) + len(item.mime_type) + len(item.attachment_id)
            for item in message.attachments
        )
        + sum(len(item.url) + len(item.title) for item in message.links)
        + len(message.reactions) * 24
        + 64
    )


def _anchor_unit_input_size(anchor_unit: AnchorUnit, config: RuntimeConfig) -> int:
    return (
        sum(_message_input_size(message, config) for message in anchor_unit.messages)
        + sum(
            min(len(block.text), config.prompt_attachment_char_limit) + 32
            for block in anchor_unit.attachment_texts
        )
        + sum(
            min(len(block.text), config.prompt_attachment_char_limit) + 32
            for block in anchor_unit.linked_file_texts
        )
    )


def _segment_batch_input_size(batch: SegmentAnalysisBatch, config: RuntimeConfig) -> int:
    return sum(
        sum(_message_input_size(message, config) for message in unit.messages)
        + sum(
            min(len(block.text), config.prompt_attachment_char_limit) + 32
            for block in unit.attachment_texts
        )
        + sum(
            min(len(block.text), config.prompt_attachment_char_limit) + 32
            for block in unit.linked_file_texts
        )
        for unit in batch.segments
    )


def _attach_anchor_attachment_texts(
    units: list[ConversationSegmentUnit],
    anchor_unit: AnchorUnit,
) -> list[ConversationSegmentUnit]:
    return [
        replace(
            unit,
            attachment_texts=[
                block
                for block in anchor_unit.attachment_texts
                if block.message_id in {message.message_id for message in unit.messages}
            ],
        )
        for unit in units
    ]


def _anchor_unit_context_signature(
    anchor_unit: AnchorUnit,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    return (
        tuple(message.message_id for message in anchor_unit.messages),
        tuple(block.attachment_id for block in anchor_unit.attachment_texts),
        tuple(block.link_id for block in anchor_unit.linked_file_texts),
    )


def _validate_anchor_batch_result(
    items: list[object],
    anchor_units: list[AnchorUnit],
) -> tuple[dict[str, AnchorAnalysisResult], list[AnchorUnit], int]:
    expected_ids = {item.anchor_unit_id for item in anchor_units}
    returned_ids = [
        item.anchor_unit_id
        for item in items
        if isinstance(getattr(item, "anchor_unit_id", None), str)
    ]
    duplicate_ids = {
        item for item in returned_ids if returned_ids.count(item) > 1
    }
    valid: dict[str, AnchorAnalysisResult] = {}
    invalid_count = 0
    for item in items:
        anchor_unit_id = getattr(item, "anchor_unit_id", "")
        analysis = getattr(item, "analysis", None)
        if (
            not isinstance(anchor_unit_id, str)
            or anchor_unit_id not in expected_ids
            or anchor_unit_id in duplicate_ids
            or not isinstance(analysis, AnchorAnalysisResult)
        ):
            invalid_count += 1
            continue
        valid[anchor_unit_id] = analysis
    missing = [item for item in anchor_units if item.anchor_unit_id not in valid]
    return valid, missing, invalid_count


def _dedupe_segment_primary_ownership(
    units: list[ConversationSegmentUnit],
) -> list[ConversationSegmentUnit]:
    owned_ids: set[str] = set()
    deduped: list[ConversationSegmentUnit] = []
    for unit in units:
        primary_ids = [item for item in unit.primary_message_ids if item not in owned_ids]
        if not primary_ids:
            continue
        owned_ids.update(primary_ids)
        context_ids = list(dict.fromkeys([*unit.context_message_ids, *(
            item for item in unit.primary_message_ids if item not in primary_ids
        )]))
        included_ids = set(primary_ids) | set(context_ids)
        deduped.append(
            replace(
                unit,
                primary_message_ids=primary_ids,
                context_message_ids=context_ids,
                self_evidence_message_ids=[
                    item for item in unit.self_evidence_message_ids if item in included_ids
                ],
                response_signals=[
                    item for item in unit.response_signals if item.message_id in included_ids
                ],
                messages=[
                    item for item in unit.messages if item.message_id in included_ids
                ],
            )
        )
    return deduped


def _keep_relation_context_out_of_event_sources(
    units: list[ConversationSegmentUnit],
    *,
    relation_context_message_ids: set[str],
) -> list[ConversationSegmentUnit]:
    """Keep externally related messages visible, but never let them support an event."""
    if not relation_context_message_ids:
        return units
    adjusted: list[ConversationSegmentUnit] = []
    for unit in units:
        primary_ids = [
            message_id
            for message_id in unit.primary_message_ids
            if message_id not in relation_context_message_ids
        ]
        if not primary_ids:
            continue
        context_ids = list(
            dict.fromkeys(
                [
                    *unit.context_message_ids,
                    *(
                        message_id
                        for message_id in unit.primary_message_ids
                        if message_id in relation_context_message_ids
                    ),
                ]
            )
        )
        included_ids = set(primary_ids) | set(context_ids)
        adjusted.append(
            replace(
                unit,
                primary_message_ids=primary_ids,
                context_message_ids=context_ids,
                self_evidence_message_ids=[
                    message_id
                    for message_id in unit.self_evidence_message_ids
                    if message_id in primary_ids
                ],
                response_signals=[
                    signal for signal in unit.response_signals if signal.message_id in primary_ids
                ],
                messages=[
                    message for message in unit.messages if message.message_id in included_ids
                ],
            )
        )
    return adjusted


def _safe_conversation_dir_name(slice_id: str) -> str:
    return slice_id.replace("/", "_").replace(":", "__")


def _safe_segment_batch_dir_name(segment_key: str) -> str:
    safe_name = _safe_conversation_dir_name(segment_key)
    max_length = 96
    if len(safe_name) <= max_length:
        return safe_name
    suffix = sha1(segment_key.encode("utf-8")).hexdigest()[:12]
    prefix = safe_name[: max_length - len(suffix) - 2].rstrip("_-")
    return f"{prefix}--{suffix}"


def run_daily_trace(target_date: str, config: RuntimeConfig) -> DailyRunResult:
    runner = DailyTraceRunner(config=config, dependencies=build_runtime_dependencies(config))
    return runner.run(target_date)


def _conversation_slice_signature(
    conversation_slice: ConversationSlice,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    return (
        tuple(message.message_id for message in conversation_slice.messages),
        tuple(block.attachment_id for block in conversation_slice.attachment_texts),
        tuple(block.link_id for block in conversation_slice.linked_file_texts),
    )


def _deliver_markdown_to_self(delivery_channel, *, self_identity, markdown_path: Path) -> tuple[str, str, str]:
    try:
        status, target = delivery_channel.deliver_to_self(
            self_identity=self_identity,
            markdown_path=markdown_path,
        )
        return status, target, ""
    except DeliveryError as exc:
        return "failed", self_identity.open_id, str(exc)


def _attach_event_file_links(
    events: list[WorkEvent],
    *,
    messages: list,
    content_resolver,
) -> list[WorkEvent]:
    message_by_id = {message.message_id: message for message in messages}
    link_by_id: dict[str, EventFileLink] = {}
    attachment_by_id: dict[str, EventFileLink] = {}
    attachment_message_id_by_id: dict[str, str] = {}
    references: list[_EventFileReference] = []
    for message in messages:
        for index, link in enumerate(content_resolver.extract_links(message), start=1):
            file_link = EventFileLink(
                url=link.url,
                title=link.title,
                link_type=link.link_type,
            )
            link_by_id[build_message_link_id(message.message_id, index)] = file_link
            references.append(
                _EventFileReference(
                    message_id=message.message_id,
                    file_link=file_link,
                    evidence_values=tuple(_link_strong_evidence_values(file_link)),
                )
            )
        for attachment in getattr(message, "attachments", []):
            file_name = attachment.file_name.strip()
            if not file_name or attachment.mime_type.startswith("image/"):
                continue
            attachment_by_id[attachment.attachment_id] = EventFileLink(
                url="",
                title=file_name,
                link_type="attachment",
            )
            attachment_message_id_by_id[attachment.attachment_id] = message.message_id
    attached: list[WorkEvent] = []

    for event in events:
        deduped: dict[str, EventFileLink] = {}
        for link_id in event.referenced_link_ids:
            link = link_by_id.get(link_id)
            if link is None:
                continue
            if not _event_supports_link(event, link):
                continue
            key = _file_link_key(link)
            existing = deduped.get(key)
            if existing is None:
                deduped[key] = link
                continue
            if existing.title.strip() or not link.title.strip():
                continue
            deduped[key] = link

        for reference in references:
            if not _event_supports_file_reference(event, reference):
                continue
            key = _file_link_key(reference.file_link)
            existing = deduped.get(key)
            if existing is None:
                deduped[key] = reference.file_link
                continue
            if existing.title.strip() or not reference.file_link.title.strip():
                continue
            deduped[key] = reference.file_link

        resolved_attachment_ids = list(event.referenced_attachment_ids)
        source_conversation_ids = {
            message_by_id[message_id].conversation_id
            for message_id in event.source_message_ids
            if message_id in message_by_id
        }
        for attachment_id, attachment in attachment_by_id.items():
            if attachment_id in resolved_attachment_ids:
                continue
            message_id = attachment_message_id_by_id.get(attachment_id, "")
            message = message_by_id.get(message_id)
            if (
                not source_conversation_ids
                or message is None
                or message.conversation_id not in source_conversation_ids
            ):
                continue
            if not _event_supports_attachment_name(event, attachment):
                continue
            resolved_attachment_ids.append(attachment_id)

        for attachment_id in resolved_attachment_ids:
            attachment = attachment_by_id.get(attachment_id)
            if attachment is None:
                continue
            deduped.setdefault(_file_link_key(attachment), attachment)

        file_links = list(deduped.values())
        file_keys = list(
            dict.fromkeys(
                [
                    *event.file_keys,
                    *(
                        key
                        for link in file_links
                        if (key := file_key_from_url(link.url))
                    ),
                    *(
                        key
                        for attachment_id in resolved_attachment_ids
                        if (key := file_key_from_attachment_id(attachment_id))
                    ),
                ]
            )
        )
        title = _make_file_references_readable(
            event.title,
            file_links,
            prefix_if_missing=True,
        )

        attached.append(
            type(event)(
                date=event.date,
                event_id=event.event_id,
                title=title,
                content=_make_file_references_readable(event.content, file_links),
                source_message_ids=list(event.source_message_ids),
                file_links=file_links,
                source_people=list(event.source_people),
                source_event_ids=list(event.source_event_ids),
                object_hint=_make_file_references_readable(event.object_hint, file_links),
                retention_reason=event.retention_reason,
                retention_detail=_make_file_references_readable(
                    event.retention_detail,
                    file_links,
                ),
                referenced_link_ids=list(event.referenced_link_ids),
                referenced_attachment_ids=resolved_attachment_ids,
                workstream_name=event.workstream_name,
                action_labels=list(event.action_labels),
                self_relations=list(event.self_relations),
                evidence_fingerprints=list(event.evidence_fingerprints),
                conversation_fingerprints=list(event.conversation_fingerprints),
                file_keys=file_keys,
            )
        )

    return attached


def _event_supports_link(event: WorkEvent, link: EventFileLink) -> bool:
    evidence_text = " ".join(
        [
            event.title,
            event.content,
            event.object_hint,
            event.retention_detail,
        ]
    ).lower()
    if not evidence_text.strip():
        return False

    for token in _link_evidence_tokens(link):
        if token in evidence_text:
            return True
    return False


def _event_supports_file_reference(
    event: WorkEvent,
    reference: _EventFileReference,
) -> bool:
    evidence_text = _event_file_evidence_text(event)
    if not evidence_text.strip():
        return False
    for value in reference.evidence_values:
        if value and value in evidence_text:
            return True
    return False


def _event_supports_attachment_name(event: WorkEvent, attachment: EventFileLink) -> bool:
    display_name = _file_display_name(attachment)
    if not display_name:
        return False
    return display_name.casefold() in _event_file_evidence_text(event).casefold()


def _event_file_evidence_text(event: WorkEvent) -> str:
    return " ".join(
        [
            event.title,
            event.content,
            event.object_hint,
            event.retention_detail,
        ]
    )


def _link_evidence_tokens(link: EventFileLink) -> list[str]:
    evidence_source = link.title.strip() or link.url
    tokens = [token.lower() for token in _LINK_TEXT_TOKEN_RE.findall(evidence_source)]
    return [
        token
        for token in tokens
        if token not in _GENERIC_LINK_HINT_TOKENS and len(token.strip()) >= 2
    ]


def _link_exact_evidence_values(link: EventFileLink) -> list[str]:
    values: list[str] = []
    for value in (link.url.strip(), link.title.strip()):
        if value:
            values.append(value)
    token = _feishu_doc_token_from_url(link.url)
    if token:
        values.append(token)
    return _dedupe_text_values(values)


def _link_strong_evidence_values(link: EventFileLink) -> list[str]:
    values: list[str] = []
    if link.url.strip():
        values.append(link.url.strip())
    token = _feishu_doc_token_from_url(link.url)
    if token:
        values.append(token)
    return _dedupe_text_values(values)


def _feishu_doc_token_from_url(url: str) -> str:
    try:
        path = urlsplit(url).path
    except ValueError:
        return ""
    match = re.search(r"/(?:docx|wiki)/([^/?#]+)", path)
    if not match:
        return ""
    return match.group(1)


def _file_link_key(link: EventFileLink) -> str:
    if link.url.strip():
        return f"url:{link.url.strip()}"
    return f"attachment:{link.title.strip()}"


def _make_file_references_readable(
    value: str,
    file_links: list[EventFileLink],
    *,
    prefix_if_missing: bool = False,
) -> str:
    result = value
    display_names = [
        display_name
        for display_name in (_file_display_name(link) for link in file_links)
        if display_name
    ]
    if not display_names:
        return result

    for link in file_links:
        display_name = _file_display_name(link)
        if not display_name:
            continue
        replacement = _quote_file_name(display_name)
        for evidence in sorted(_file_readable_evidence_values(link), key=len, reverse=True):
            if not evidence:
                continue
            result = _replace_unquoted(result, evidence, replacement)

    if prefix_if_missing and not any(
        _contains_file_display_name(result, display_name)
        for display_name in display_names
    ):
        result = f"{_quote_file_name(display_names[0])}{result}"
    return result


def _file_readable_evidence_values(link: EventFileLink) -> list[str]:
    values = _link_exact_evidence_values(link)
    display_name = _file_display_name(link)
    if display_name:
        values.append(display_name)
    return _dedupe_text_values(values)


def _file_display_name(link: EventFileLink) -> str:
    return link.title.strip() or link.url.strip()


def _quote_file_name(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("《") and stripped.endswith("》"):
        return stripped
    return f"《{stripped}》"


def _replace_unquoted(value: str, needle: str, replacement: str) -> str:
    if not needle or needle == replacement:
        return value
    pattern = re.compile(rf"(?<!《){re.escape(needle)}(?!》)")
    return pattern.sub(replacement, value)


def _contains_file_display_name(value: str, display_name: str) -> bool:
    quoted = _quote_file_name(display_name)
    return quoted in value or display_name in value


def _dedupe_text_values(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        stripped = value.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        deduped.append(stripped)
    return deduped


def _sort_events_for_output(events: list[WorkEvent], *, messages: list) -> list[WorkEvent]:
    message_by_id = {message.message_id: message for message in messages}

    def _event_sort_key(event: WorkEvent) -> tuple[str, str, str]:
        source_times = [
            message_by_id[message_id].send_time
            for message_id in event.source_message_ids
            if message_id in message_by_id
        ]
        first_time = min(source_times) if source_times else ""
        first_message_id = event.source_message_ids[0] if event.source_message_ids else ""
        return (first_time, first_message_id, event.event_id)

    return sorted(events, key=_event_sort_key)
