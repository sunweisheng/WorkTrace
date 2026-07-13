from __future__ import annotations

from dataclasses import dataclass, replace
import logging
from pathlib import Path
import re
from time import perf_counter
from urllib.parse import urlsplit

from .config import RuntimeConfig
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
    SegmentAnalysisBatch,
    SourceBackedEventDraft,
    ConversationSlice,
    SelfIdentity,
    WorkstreamAssignment,
    WorkstreamAssignmentResult,
)
from .analyzers.output_schemas import workstream_assignment_output_schema
from .analyzers.prompts import (
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
from .pipeline.direct_relation_filter import filter_self_related_candidate_drafts
from .pipeline.event_merge import build_work_events
from .pipeline.filtering import filter_messages
from .pipeline.retention_filter import (
    filter_retained_candidate_drafts,
    filter_retained_merged_drafts,
    filter_retained_work_events,
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


@dataclass
class DailyTraceRunner:
    config: RuntimeConfig
    dependencies: RuntimeDependencies
    reaction_catalog: ReactionCatalog | None = None

    def __post_init__(self) -> None:
        if self.reaction_catalog is None:
            source_id = getattr(self.dependencies.chat_source, "source_id", "feishu")
            self.reaction_catalog = ReactionCatalogStore.from_config(self.config).load(source_id)

    def run(self, target_date: str) -> DailyRunResult:
        run_started_at = perf_counter()
        warning_messages: list[str] = []
        skipped_slice_count = 0

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
                    filter_retained_candidate_drafts(all_candidates)
                )
                warning_messages.extend(retention_candidate_warnings)

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
            events, retention_event_warnings = filter_retained_work_events(events)
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
            ),
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
    ) -> DailyRunResult:
        warning_messages = warning_messages or []
        write_result = self.dependencies.event_store.replace_day(
            target_date,
            [],
            owner_display_name=self_identity.display_name,
        )
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
        )

    def _finish_run(
        self,
        run_started_at: float,
        result: DailyRunResult,
    ) -> DailyRunResult:
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
        )
        return result

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

        image_summary_func = getattr(self.dependencies.content_resolver, "summarize_images", None)
        try:
            image_summary_messages = sorted(
                messages,
                key=lambda item: item.sender_open_id != self_identity.open_id,
            )
            image_summaries = (
                image_summary_func(image_summary_messages)
                if callable(image_summary_func)
                else []
            )
        except Exception as exc:
            image_summaries = []
            warnings.append(f"Skipped image summaries: {exc}")
        image_summaries_by_message: dict[str, list] = {}
        for block in image_summaries:
            image_summaries_by_message.setdefault(block.message_id, []).append(block)

        anchor_units = group_anchor_units(
            messages,
            self_identity.open_id,
            before_limit=30,
            after_limit=30,
            reaction_catalog=self.reaction_catalog,
        )
        anchors_by_conversation: dict[str, list] = {}
        for anchor_unit in anchor_units:
            anchors_by_conversation.setdefault(anchor_unit.conversation_id, []).append(
                anchor_unit
            )

        for conversation_id, conversation_anchors in sorted(
            anchors_by_conversation.items()
        ):
            if not conversation_anchors:
                continue
            conversation_anchors = [
                replace(
                    self._hydrate_anchor_link_titles(item),
                    attachment_texts=_image_summaries_for_messages(
                        image_summaries_by_message,
                        item.messages,
                    ),
                )
                for item in conversation_anchors
            ]
            conversation_name = conversation_anchors[0].conversation_name
            conversation_units: list[ConversationSegmentUnit] = []
            fallback_required = False
            segmentation_cache: dict[
                tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]],
                tuple[list[ConversationSegmentUnit], list[str], str, int, str],
            ] = {}
            segmentation_failure_counts: dict[str, int] = {}
            segmentation_circuit_open = False
            skipped_anchor_count = 0
            for anchor_index, anchor_unit in enumerate(conversation_anchors, start=1):
                if segmentation_circuit_open:
                    skipped_anchor_count += 1
                    continue
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
                units: list[ConversationSegmentUnit] = []
                segmentation_warnings: list[str] = []
                segmentation_error = ""
                retry_round = 0
                failure_category = ""
                segmentation_started_at = perf_counter()
                window_signature = _anchor_unit_context_signature(anchor_unit)
                cached = segmentation_cache.get(window_signature)
                if cached is not None:
                    (
                        units,
                        segmentation_warnings,
                        segmentation_error,
                        retry_round,
                        failure_category,
                    ) = cached
                else:
                    for retry_round in range(self.config.anchor_retry_limit + 1):
                        segmentation_started_at = perf_counter()
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
                            )
                        except AnalyzerProtocolError as exc:
                            model_call_count += 1
                            segmentation_error = str(exc)
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
                        units = [
                            replace(
                                unit,
                                attachment_texts=_image_summaries_for_messages(
                                    image_summaries_by_message,
                                    unit.messages,
                                ),
                            )
                            for unit in units
                        ]
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
                            break
                    if not units:
                        failure_category = (
                            "analyzer_protocol_failure"
                            if segmentation_error
                            else "segmentation_validation_failure"
                        )
                    segmentation_cache[window_signature] = (
                        units,
                        segmentation_warnings,
                        segmentation_error,
                        retry_round,
                        failure_category,
                    )

                if not units:
                    fallback_required = True
                    if cached is None:
                        warnings.extend(segmentation_warnings)
                        if segmentation_error:
                            warnings.append(
                                "Skipped anchor after segmentation retries failed: "
                                f"{segmentation_error}"
                            )
                        else:
                            warnings.append("Skipped anchor after invalid segmentation retries.")
                        segmentation_failure_counts[failure_category] = (
                            segmentation_failure_counts.get(failure_category, 0) + 1
                        )
                        if (
                            segmentation_failure_counts[failure_category]
                            >= self.config.conversation_segmentation_failure_threshold
                        ):
                            segmentation_circuit_open = True
                            warnings.append(
                                "Stopped remaining anchor segmentation after repeated "
                                f"{failure_category}."
                            )
                    continue

                conversation_units.extend(
                    replace(
                        unit,
                        segment_id=f"anchor-{anchor_index:03d}:{unit.segment_id}",
                    )
                    for unit in units
                    if set(unit.primary_message_ids)
                    & set(anchor_unit.anchor_message_ids)
                )
                warnings.extend(segmentation_warnings)
                log_timing(
                    logger,
                    "runner.stage.completed",
                    segmentation_started_at,
                    stage="segment_conversation",
                    conversation_id=conversation_id,
                    segment_count=len(units),
                    anchor_index=anchor_index,
                    anchor_count=len(conversation_anchors),
                    anchor_message_count=len(anchor_unit.anchor_message_ids),
                    input_message_count=len(segmentation_messages),
                    retry_round=retry_round,
                )
            if skipped_anchor_count:
                warnings.append(
                    "Skipped remaining anchor segmentation windows after circuit open: "
                    f"{skipped_anchor_count}."
                )
            conversation_units = _dedupe_segment_primary_ownership(conversation_units)
            if conversation_units:
                conversation_slices.extend(
                    segment_unit_to_slice(unit) for unit in conversation_units
                )
                for batch in pack_segment_units(
                    target_date=target_date,
                    self_open_id=self_identity.open_id,
                    self_display_name=self_identity.display_name,
                    units=conversation_units,
                    config=self.config,
                ):
                    (
                        batch_candidates,
                        batch_warnings,
                        batch_skipped_count,
                        batch_call_count,
                    ) = self._analyze_segment_batch_with_retry(
                        batch=batch,
                        self_identity=self_identity,
                    )
                    candidates.extend(batch_candidates)
                    warnings.extend(batch_warnings)
                    skipped_segment_count += batch_skipped_count
                    model_call_count += batch_call_count

            if fallback_required:
                warnings.append(
                    "Anchor segmentation retries were exhausted; running full-conversation anchor fallback."
                )
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

        return (
            candidates,
            conversation_slices,
            warnings,
            skipped_segment_count,
            model_call_count,
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
            try:
                result = self.dependencies.analyzer.analyze_anchor_batch(
                    target_date,
                    anchor_units,
                )
                call_count += 1
            except AnalyzerProtocolError:
                call_count += 1
                continue

            saw_response = True
            final_valid, final_missing, invalid_count = _validate_anchor_batch_result(
                result.results,
                anchor_units,
            )
            if invalid_count:
                warnings.append(
                    "Filtered invalid anchor fallback batch result."
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
    ) -> tuple[list[SourceBackedEventDraft], list[str], int, int]:
        warnings: list[str] = []
        call_count = 0

        for attempt in range(2):
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
                if attempt == 0:
                    warnings.append("Segment batch failed; retrying the same batch once.")
                    continue
                warnings.append("Segment batch failed twice; retrying its segments separately.")
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
            try:
                result = self.dependencies.analyzer.analyze_segment_batch(single_batch)
                call_count += 1
            except AnalyzerProtocolError:
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
                )
            )
            call_count += nested_call_count
            candidates.extend(unit_candidates)
            warnings.extend(unit_warnings)
            skipped_count += unit_skipped_count

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
                warning_sink=warnings,
            )
            if validated.context_requests:
                if not allow_context_expansion:
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
                allow_context_expansion=False,
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
            / _safe_conversation_dir_name(segment_key)
            / f"analysis-{retry_round + 1:02d}"
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


def _image_summaries_for_messages(
    summaries_by_message: dict[str, list],
    messages: list[NormalizedMessage],
) -> list:
    message_ids = {item.message_id for item in messages}
    return [
        summary
        for message_id, summaries in summaries_by_message.items()
        if message_id in message_ids
        for summary in summaries
    ]


def _anchor_unit_to_slice(anchor_unit: AnchorUnit) -> ConversationSlice:
    return ConversationSlice(
        slice_id=f"{anchor_unit.conversation_id}:anchor:{anchor_unit.anchor_unit_id}",
        conversation_id=anchor_unit.conversation_id,
        conversation_name=anchor_unit.conversation_name,
        anchor_message_ids=list(anchor_unit.anchor_message_ids),
        in_day_message_ids=[item.message_id for item in anchor_unit.messages],
        messages=list(anchor_unit.messages),
        attachment_texts=list(anchor_unit.attachment_texts),
        linked_file_texts=list(anchor_unit.linked_file_texts),
    )


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


def _safe_conversation_dir_name(slice_id: str) -> str:
    return slice_id.replace("/", "_").replace(":", "__")
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
    link_by_id: dict[str, EventFileLink] = {}
    attachment_by_id: dict[str, EventFileLink] = {}
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

        for attachment_id in event.referenced_attachment_ids:
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
                        for attachment_id in event.referenced_attachment_ids
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
                referenced_attachment_ids=list(event.referenced_attachment_ids),
                workstream_name=event.workstream_name,
                action_labels=list(event.action_labels),
                self_relations=list(event.self_relations),
                evidence_fingerprints=list(event.evidence_fingerprints),
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
