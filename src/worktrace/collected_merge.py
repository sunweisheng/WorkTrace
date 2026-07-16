from __future__ import annotations

import json
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from time import sleep
from typing import Any, Sequence

from .config import RuntimeConfig
from .constants import DailyRunStatus
from .delivery.feishu_cli import FeishuCliSelfDelivery
from .errors import (
    AnalyzerProtocolError,
    DeliveryError,
    RetryableAnalyzerProtocolError,
    StoreWriteError,
)
from .factories import AnalyzerFactory
from .analyzers.base import Analyzer
from .analyzers.prompts import (
    build_collected_grouping_prompt,
    build_collected_merge_prompt,
    build_collected_review_prompt,
    build_collected_render_prompt,
)
from .models import (
    CollectedGroupingGroup,
    CollectedGroupingResult,
    CollectedFactItem,
    CollectedMergeGroup,
    CollectedMergeOutput,
    CollectedMergeQualitySummary,
    CollectedMergeResult,
    CollectedMergeRunResult,
    CollectedSourceEvent,
    DayDocument,
    EventFileLink,
    SelfIdentity,
    WorkEvent,
)
from .pipeline.sensitive_filter import (
    filter_work_events_with_diagnostics,
)
from .pipeline.retention_filter import (
    RETENTION_REASONS,
    filter_retained_work_events,
    retention_rejection_reason_for_event,
)
from .stores.markdown import MarkdownEventStore
from .utils.dates import now_iso
from .utils.filenames import (
    build_merged_markdown_filename,
    parse_worktrace_markdown_filename,
)
from .utils.hashing import file_key_from_url, stable_event_id
from .utils.json_io import dump_json
from .utils.text import choose_preferred_text, clean_text, merge_content_texts
from .utils.token_estimation import estimate_text_tokens


@dataclass
class CollectedMergeRunner:
    config: RuntimeConfig
    analyzer: Any | None = None
    cwd: Path | None = None
    command_runner: Any | None = None
    delivery_channel: Any | None = None
    self_identity_resolver: Any | None = None
    sleep_func: Any | None = None

    def __post_init__(self) -> None:
        if self.cwd is None:
            self.cwd = Path.cwd()
        if self.analyzer is None:
            self.analyzer = AnalyzerFactory.create_default(self.config)
        if self.command_runner is None:
            self.command_runner = self._run_command
        if self.delivery_channel is None:
            self.delivery_channel = FeishuCliSelfDelivery(
                command_runner=self.command_runner,
                cwd=self.cwd,
            )
        if self.self_identity_resolver is None:
            self.self_identity_resolver = self._resolve_self_identity
        if self.sleep_func is None:
            self.sleep_func = sleep
        self.store = MarkdownEventStore(config=self.config)
        self._collected_merge_trace_dir: Path | None = None
        self._collected_merge_trace_steps: list[dict[str, Any]] = []
        self._collected_merge_trace_call_index = 0
        self._collected_merge_source_audit: list[dict[str, Any]] = []
        self._collected_merge_filter_diagnostics: list[dict[str, Any]] = []
        self._collected_merge_failure_warnings: list[str] = []
        self._collected_quality_counters: Counter[str] = Counter()

    def run(self, target_date: str) -> CollectedMergeRunResult:
        input_dir = self.build_input_dir(target_date)
        try:
            self_identity = self.self_identity_resolver()
        except (OSError, ValueError, StoreWriteError) as exc:
            return CollectedMergeRunResult(
                status=DailyRunStatus.FAILED.value,
                target_date=target_date,
                input_dir=str(input_dir.resolve()),
                output_path=None,
                source_file_count=0,
                source_event_count=0,
                merged_event_count=0,
                skipped_file_count=0,
                partial_file_count=0,
                warning_messages=[str(exc)],
                self_delivery_status="",
                self_delivery_target="",
                self_delivery_error="",
                outputs=[],
            )
        child_dirs = (
            [
                child
                for child in sorted(input_dir.iterdir())
                if child.is_dir() and not child.name.startswith(".")
            ]
            if input_dir.exists()
            else []
        )
        preflight_failure = self._preflight_conversation_evidence(
            target_date,
            self_identity=self_identity,
            input_dir=input_dir,
            child_dirs=child_dirs,
        )
        if preflight_failure is not None:
            return preflight_failure

        outputs = [
            self._run_one_directory(
                target_date,
                input_dir,
                self_identity=self_identity,
                ignored_subdirectories={child.name for child in child_dirs},
            )
        ]
        for child in child_dirs:
            outputs.append(
                self._run_one_directory(
                    target_date,
                    child,
                    self_identity=self_identity,
                    ignored_subdirectories=set(),
                )
            )

        warning_messages = [
            warning
            for output in outputs
            for warning in output.warning_messages
        ]
        failed_outputs = [output for output in outputs if output.output_path is None]
        if failed_outputs:
            status = DailyRunStatus.FAILED.value
        elif warning_messages:
            status = DailyRunStatus.SUCCESS_WITH_WARNINGS.value
        else:
            status = DailyRunStatus.SUCCESS.value

        first_output = outputs[0]
        delivery_errors = [
            output.self_delivery_error for output in outputs if output.self_delivery_error
        ]
        return CollectedMergeRunResult(
            status=status,
            target_date=target_date,
            input_dir=str(input_dir.resolve()),
            output_path=first_output.output_path,
            source_file_count=sum(output.source_file_count for output in outputs),
            source_event_count=sum(output.source_event_count for output in outputs),
            merged_event_count=sum(output.merged_event_count for output in outputs),
            skipped_file_count=sum(output.skipped_file_count for output in outputs),
            partial_file_count=sum(output.partial_file_count for output in outputs),
            quality_summary=aggregate_collected_quality_summaries(
                [output.quality_summary for output in outputs]
            ),
            warning_messages=warning_messages,
            self_delivery_status=summarize_self_delivery_status(outputs),
            self_delivery_target=first_output.self_delivery_target,
            self_delivery_error="; ".join(delivery_errors),
            outputs=outputs,
        )

    def _preflight_conversation_evidence(
        self,
        target_date: str,
        *,
        self_identity: SelfIdentity,
        input_dir: Path,
        child_dirs: list[Path],
    ) -> CollectedMergeRunResult | None:
        scope_specs = [
            (
                input_dir,
                {child.name for child in child_dirs},
            ),
            *((child, set()) for child in child_dirs),
        ]
        inspected: list[tuple[Path, int, int, int, int, list[str]]] = []
        missing_warnings: list[str] = []

        for scope_dir, ignored_subdirectories in scope_specs:
            output_path = scope_dir / build_merged_markdown_filename(
                target_date,
                self_identity.display_name or self_identity.open_id,
            )
            (
                source_events,
                source_file_count,
                skipped_file_count,
                partial_file_count,
                _,
                _,
            ) = self._read_source_events(
                target_date,
                scope_dir,
                output_path=output_path,
                ignored_subdirectories=ignored_subdirectories,
            )
            missing_by_file = Counter(
                item.source_file
                for item in source_events
                if not item.event.conversation_fingerprints
            )
            scope_warnings = [
                (
                    "Missing version 2 conversation evidence: "
                    f"{source_file} ({count} events). Regenerate this markdown "
                    "before running merge-collected."
                )
                for source_file, count in sorted(missing_by_file.items())
            ]
            missing_warnings.extend(scope_warnings)
            inspected.append(
                (
                    scope_dir,
                    source_file_count,
                    len(source_events),
                    skipped_file_count,
                    partial_file_count,
                    scope_warnings,
                )
            )

        if not missing_warnings:
            return None

        outputs = [
            CollectedMergeOutput(
                input_dir=str(scope_dir.resolve()),
                output_path=None,
                source_file_count=source_file_count,
                source_event_count=source_event_count,
                merged_event_count=0,
                skipped_file_count=skipped_file_count,
                partial_file_count=partial_file_count,
                quality_summary=CollectedMergeQualitySummary(
                    input_event_count=source_event_count,
                    filtered_event_count=source_event_count,
                ),
                warning_messages=(
                    scope_warnings
                    or [
                        "Collected merge stopped before this scope because another "
                        "scope has missing version 2 conversation evidence."
                    ]
                ),
            )
            for (
                scope_dir,
                source_file_count,
                source_event_count,
                skipped_file_count,
                partial_file_count,
                scope_warnings,
            ) in inspected
        ]
        first_output = outputs[0]
        return CollectedMergeRunResult(
            status=DailyRunStatus.FAILED.value,
            target_date=target_date,
            input_dir=str(input_dir.resolve()),
            output_path=None,
            source_file_count=sum(item.source_file_count for item in outputs),
            source_event_count=sum(item.source_event_count for item in outputs),
            merged_event_count=0,
            skipped_file_count=sum(item.skipped_file_count for item in outputs),
            partial_file_count=sum(item.partial_file_count for item in outputs),
            quality_summary=aggregate_collected_quality_summaries(
                [output.quality_summary for output in outputs]
            ),
            warning_messages=missing_warnings,
            self_delivery_status="",
            self_delivery_target=first_output.self_delivery_target,
            self_delivery_error="",
            outputs=outputs,
        )

    def _run_one_directory(
        self,
        target_date: str,
        input_dir: Path,
        *,
        self_identity: SelfIdentity,
        ignored_subdirectories: set[str],
    ) -> CollectedMergeOutput:
        output_path = input_dir / build_merged_markdown_filename(
            target_date,
            self_identity.display_name or self_identity.open_id,
        )
        self._collected_quality_counters = Counter()
        self._start_collected_merge_trace(target_date, input_dir)
        warning_messages: list[str] = []
        (
            source_events,
            source_file_count,
            skipped_file_count,
            partial_file_count,
            read_warnings,
            source_audit,
        ) = self._read_source_events(
            target_date,
            input_dir,
            output_path=output_path,
            ignored_subdirectories=ignored_subdirectories,
        )
        warning_messages.extend(read_warnings)
        parsed_source_events = list(source_events)
        source_events, source_filter_warnings, source_filter_diagnostics = (
            self._filter_source_events(source_events)
        )
        warning_messages.extend(source_filter_warnings)
        before_retention_events = list(source_events)
        source_events, retention_source_warnings = self._filter_retained_source_events(
            source_events,
        )
        warning_messages.extend(retention_source_warnings)
        retention_diagnostics = self._build_source_retention_diagnostics(
            before_retention_events,
            source_events,
        )
        self._collected_merge_filter_diagnostics = [
            *source_filter_diagnostics,
            *retention_diagnostics,
        ]
        self._collected_merge_source_audit = self._finalize_source_audit(
            source_audit,
            parsed_source_events=parsed_source_events,
            model_input_events=source_events,
            filter_diagnostics=self._collected_merge_filter_diagnostics,
        )
        self._write_collected_merge_source_audit(
            target_date=target_date,
            input_dir=input_dir,
            source_file_count=source_file_count,
            skipped_file_count=skipped_file_count,
            partial_file_count=partial_file_count,
            parsed_event_count=len(parsed_source_events),
            model_input_event_count=len(source_events),
        )
        source_events, owner_source_warnings = self._mark_merge_owner_sources(
            source_events,
            merge_owner_person=self_identity.display_name,
            input_dir=input_dir,
        )
        warning_messages.extend(owner_source_warnings)

        merged_events: list[WorkEvent] = []
        if source_events:
            try:
                merged_events, merge_warnings = self._merge_source_events(
                    target_date,
                    source_events,
                    merge_owner_person=self_identity.display_name,
                )
                warning_messages.extend(merge_warnings)
            except (AnalyzerProtocolError, ValueError) as exc:
                warning_messages.extend(
                    warning
                    for warning in self._collected_merge_failure_warnings
                    if warning not in warning_messages
                )
                warning_messages.append(str(exc))
                quality_summary = build_collected_quality_summary(
                    parsed_source_events,
                    source_events,
                    [],
                    counters=self._collected_quality_counters,
                )
                output = CollectedMergeOutput(
                    input_dir=str(input_dir.resolve()),
                    output_path=None,
                    source_file_count=source_file_count,
                    source_event_count=len(source_events),
                    merged_event_count=0,
                    skipped_file_count=skipped_file_count,
                    partial_file_count=partial_file_count,
                    quality_summary=quality_summary,
                    warning_messages=warning_messages,
                )
                self._write_collected_merge_trace_summary(
                    status=DailyRunStatus.FAILED.value,
                    target_date=target_date,
                    input_dir=input_dir,
                    output_path=None,
                    source_file_count=source_file_count,
                    source_event_count=len(source_events),
                    merged_event_count=0,
                    skipped_file_count=skipped_file_count,
                    partial_file_count=partial_file_count,
                    warning_messages=warning_messages,
                    quality_summary=quality_summary,
                )
                return output
        else:
            warning_messages.append("No valid source events found.")

        quality_summary = build_collected_quality_summary(
            parsed_source_events,
            source_events,
            merged_events,
            counters=self._collected_quality_counters,
        )

        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            day_doc = self.store.render_day_document(
                day_doc=DayDocument(
                    date=target_date,
                    events=merged_events,
                    generated_at=now_iso(self.config.timezone),
                )
            )
            output_path.write_text(day_doc, encoding="utf-8")
        except OSError as exc:
            warning_messages.append(f"Failed to write merged markdown: {output_path}")
            self._write_collected_merge_trace_summary(
                status=DailyRunStatus.FAILED.value,
                target_date=target_date,
                input_dir=input_dir,
                output_path=None,
                source_file_count=source_file_count,
                source_event_count=len(source_events),
                merged_event_count=0,
                skipped_file_count=skipped_file_count,
                partial_file_count=partial_file_count,
                warning_messages=warning_messages,
                quality_summary=quality_summary,
            )
            return CollectedMergeOutput(
                input_dir=str(input_dir.resolve()),
                output_path=None,
                source_file_count=source_file_count,
                source_event_count=len(source_events),
                merged_event_count=0,
                skipped_file_count=skipped_file_count,
                partial_file_count=partial_file_count,
                quality_summary=quality_summary,
                warning_messages=warning_messages,
            )

        self_delivery_status, self_delivery_target, self_delivery_error = (
            _deliver_markdown_to_self(
                self.delivery_channel,
                self_identity=self_identity,
                markdown_path=output_path,
            )
        )
        if self_delivery_error:
            warning_messages.append(self_delivery_error)

        self._write_collected_merge_trace_summary(
            status=(
                DailyRunStatus.SUCCESS_WITH_WARNINGS.value
                if warning_messages
                else DailyRunStatus.SUCCESS.value
            ),
            target_date=target_date,
            input_dir=input_dir,
            output_path=output_path,
            source_file_count=source_file_count,
            source_event_count=len(source_events),
            merged_event_count=len(merged_events),
            skipped_file_count=skipped_file_count,
            partial_file_count=partial_file_count,
            warning_messages=warning_messages,
            quality_summary=quality_summary,
        )

        return CollectedMergeOutput(
            input_dir=str(input_dir.resolve()),
            output_path=str(output_path.resolve()),
            source_file_count=source_file_count,
            source_event_count=len(source_events),
            merged_event_count=len(merged_events),
            skipped_file_count=skipped_file_count,
            partial_file_count=partial_file_count,
            quality_summary=quality_summary,
            warning_messages=warning_messages,
            self_delivery_status=self_delivery_status,
            self_delivery_target=self_delivery_target,
            self_delivery_error=self_delivery_error,
        )

    def _merge_source_events(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        *,
        merge_owner_person: str,
    ) -> tuple[list[WorkEvent], list[str]]:
        if hasattr(self.analyzer, "group_collected_events"):
            return self._merge_source_events_two_stage(
                target_date,
                source_events,
            )
        return self._merge_source_events_single_stage(
            target_date,
            source_events,
            merge_owner_person=merge_owner_person,
        )

    def _merge_source_events_two_stage(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
    ) -> tuple[list[WorkEvent], list[str]]:
        deterministic_groups, deterministic_warnings = self._build_deterministic_groups(
            source_events,
        )
        grouping_result, grouping_warnings = self._invoke_collected_grouping_with_retry(
            target_date,
            source_events,
            deterministic_groups,
        )
        grouping_result, repair_warnings = repair_collected_grouping_result(
            grouping_result,
            source_events,
            deterministic_groups,
        )
        grouping_result, review_warnings = self._review_high_risk_groups(
            target_date,
            source_events,
            grouping_result,
        )
        source_by_id = {item.draft_id: item for item in source_events}
        multi_groups = [
            group for group in grouping_result.groups if len(group.draft_ids) > 1
        ]
        singleton_groups = [
            group for group in grouping_result.groups if len(group.draft_ids) == 1
        ]
        retry_warnings: list[str] = []
        rendered_groups: list[CollectedMergeGroup] = []
        if multi_groups:
            rendered_groups, retry_warnings = self._render_collected_multi_groups(
                target_date,
                source_events,
                multi_groups,
            )

        rendered_groups.extend(
            _build_singleton_collected_group(source_by_id[group.draft_ids[0]], index)
            for index, group in enumerate(singleton_groups, start=1)
        )
        merged_events, final_warnings = self._finalize_collected_merge_result(
            target_date,
            source_events,
            CollectedMergeResult(groups=rendered_groups),
        )
        return merged_events, [
            *deterministic_warnings,
            *grouping_warnings,
            *repair_warnings,
            *review_warnings,
            *retry_warnings,
            *final_warnings,
        ]

    def _review_high_risk_groups(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        grouping_result: CollectedGroupingResult,
    ) -> tuple[CollectedGroupingResult, list[str]]:
        if not self.config.high_risk_review_enabled:
            return grouping_result, []

        source_by_id = {item.draft_id: item for item in source_events}
        review_method = getattr(self.analyzer, "review_collected_group", None)
        review_implementation = getattr(review_method, "__func__", review_method)
        has_review_capability = bool(
            callable(review_method)
            and review_implementation is not Analyzer.review_collected_group
        )
        reviewed_groups: list[CollectedGroupingGroup] = []
        warnings: list[str] = []
        for group in grouping_result.groups:
            items = [
                source_by_id[draft_id]
                for draft_id in group.draft_ids
                if draft_id in source_by_id
            ]
            reasons = self._collected_group_review_reasons(group, items)
            if not reasons:
                reviewed_groups.append(group)
                continue

            self._collected_quality_counters["high_risk_group_count"] += 1
            self._collected_quality_counters["review_required"] = 1
            if not has_review_capability:
                reviewed_groups.append(group)
                warnings.append(
                    "High-risk collected group could not be reviewed because the "
                    f"analyzer has no review capability: group={group.group_id} "
                    f"reasons={reasons}."
                )
                continue

            try:
                reviewed, review_warnings = (
                    self._review_collected_group_with_batching(
                        target_date,
                        items,
                        group,
                        reasons=reasons,
                        depth=0,
                    )
                )
            except NotImplementedError:
                reviewed_groups.append(group)
                warnings.append(
                    "High-risk collected group could not be reviewed because the "
                    f"analyzer has no review capability: group={group.group_id} "
                    f"reasons={reasons}."
                )
                continue
            self._collected_quality_counters["reviewed_group_count"] += 1
            self._collected_quality_counters["review_split_group_count"] += int(
                len(reviewed.groups) > 1
            )
            reviewed_groups.extend(reviewed.groups)
            warnings.extend(review_warnings)
        return CollectedGroupingResult(groups=reviewed_groups), warnings

    def _collected_group_review_reasons(
        self,
        group: CollectedGroupingGroup,
        source_events: list[CollectedSourceEvent],
    ) -> list[str]:
        reasons: list[str] = []
        if len(group.draft_ids) >= self.config.high_risk_source_event_count:
            reasons.append("source_event_count")
        if (
            len({item.source_file for item in source_events})
            >= self.config.high_risk_source_file_count
        ):
            reasons.append("source_file_count")
        if (
            self.config.review_cross_batch_groups
            and "cross_batch" in group.risk_flags
        ):
            reasons.append("cross_batch")
        if self.config.review_repaired_groups and group.was_repaired:
            reasons.append("repaired_group")
        workstreams = {
            "".join(item.event.workstream_name.casefold().split())
            for item in source_events
            if item.event.workstream_name.strip()
        }
        if self.config.review_workstream_conflicts and len(workstreams) > 1:
            reasons.append("workstream_conflict")
        return reasons

    def _review_collected_group_with_batching(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        candidate_group: CollectedGroupingGroup,
        *,
        reasons: list[str],
        depth: int,
    ) -> tuple[CollectedGroupingResult, list[str]]:
        prompt_tokens = _estimate_prepared_model_prompt_tokens(
            build_collected_review_prompt(
                target_date,
                source_events,
                candidate_group,
                config=self.config,
                review_reasons=reasons,
            )
        )
        if prompt_tokens <= self.config.max_model_input_tokens:
            return self._invoke_collected_review_with_retry(
                target_date,
                source_events,
                candidate_group,
                reasons=reasons,
            )

        batches = self._pack_collected_review_batches(
            target_date,
            source_events,
            candidate_group,
            reasons=reasons,
        )
        if len(batches) <= 1:
            if len(source_events) != 1:
                raise ValueError(
                    "Collected review prompt exceeds max_model_input_tokens and "
                    "cannot be split further: "
                    f"estimated_tokens={prompt_tokens} "
                    f"limit={self.config.max_model_input_tokens} "
                    f"group={candidate_group.group_id}"
                )
            rendered, render_warnings = self._render_oversized_locked_group(
                target_date,
                source_events,
                CollectedGroupingGroup(
                    group_id=f"{candidate_group.group_id}-review-summary",
                    draft_ids=[source_events[0].draft_id],
                ),
                depth=depth,
            )
            original = source_events[0]
            summarized_event = replace(
                original,
                event=replace(
                    original.event,
                    title=rendered.title,
                    content=rendered.content,
                    object_hint=rendered.object_hint,
                    retention_reason=rendered.retention_reason,
                    retention_detail=rendered.retention_detail,
                ),
                prompt_original_content_chars=len(clean_text(original.event.content)),
            )
            self._collected_quality_counters["shortened_prompt_count"] += 1
            reviewed, review_warnings = self._invoke_collected_review_with_retry(
                target_date,
                [summarized_event],
                candidate_group,
                reasons=reasons,
            )
            return reviewed, [
                "Used hierarchical content summary for oversized high-risk review: "
                f"group={candidate_group.group_id}.",
                *render_warnings,
                *review_warnings,
            ]

        self._collected_quality_counters["shortened_prompt_count"] += 1
        warnings = [
            "Using relation-priority high-risk review batches: "
            f"group={candidate_group.group_id} depth={depth + 1} "
            f"batches={len(batches)} input_limit_tokens="
            f"{self.config.max_model_input_tokens}."
        ]
        partial_groups: list[CollectedGroupingGroup] = []
        for batch_index, batch_events in enumerate(batches, start=1):
            batch_group = replace(
                candidate_group,
                group_id=f"{candidate_group.group_id}-batch-{batch_index}",
                draft_ids=[item.draft_id for item in batch_events],
            )
            reviewed, batch_warnings = self._review_collected_group_with_batching(
                target_date,
                batch_events,
                batch_group,
                reasons=reasons,
                depth=depth + 1,
            )
            partial_groups.extend(reviewed.groups)
            warnings.extend(batch_warnings)

        if depth >= 3:
            warnings.append(
                "Stopped high-risk review reconciliation after 3 levels; "
                "reviewed batch groups were kept separate."
            )
            return CollectedGroupingResult(groups=partial_groups), warnings

        summary_events, original_ids_by_summary = build_grouping_summary_events(
            target_date,
            source_events,
            partial_groups,
            depth=depth + 1,
        )
        summary_group = CollectedGroupingGroup(
            group_id=f"{candidate_group.group_id}-reconciliation-{depth + 1}",
            draft_ids=[item.draft_id for item in summary_events],
            risk_flags=["cross_batch"],
        )
        reconciled, reconciliation_warnings = (
            self._review_collected_group_with_batching(
                target_date,
                summary_events,
                summary_group,
                reasons=_dedupe([*reasons, "cross_batch"]),
                depth=depth + 1,
            )
        )
        warnings.extend(reconciliation_warnings)
        expanded_groups = [
            replace(
                group,
                group_id=f"{candidate_group.group_id}-review-{index}",
                draft_ids=_dedupe(
                    [
                        original_id
                        for summary_id in group.draft_ids
                        for original_id in original_ids_by_summary.get(summary_id, [])
                    ]
                ),
                risk_flags=_dedupe([*group.risk_flags, "cross_batch"]),
            )
            for index, group in enumerate(reconciled.groups, start=1)
        ]
        expanded = CollectedGroupingResult(groups=expanded_groups)
        partition_error = collected_grouping_partition_error(
            expanded,
            candidate_group.draft_ids,
        )
        if partition_error:
            raise AnalyzerProtocolError(
                "High-risk review reconciliation did not preserve source coverage: "
                f"group={candidate_group.group_id} {partition_error}"
            )
        return expanded, warnings

    def _pack_collected_review_batches(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        candidate_group: CollectedGroupingGroup,
        *,
        reasons: list[str],
    ) -> list[list[CollectedSourceEvent]]:
        input_limit = self.config.max_model_input_tokens

        def prompt_tokens(events: list[CollectedSourceEvent]) -> int:
            batch_group = replace(
                candidate_group,
                draft_ids=[item.draft_id for item in events],
            )
            return _estimate_prepared_model_prompt_tokens(
                build_collected_review_prompt(
                    target_date,
                    events,
                    batch_group,
                    config=self.config,
                    review_reasons=reasons,
                )
            )

        components = build_collected_relation_components(source_events, [])
        batches: list[list[CollectedSourceEvent]] = []
        current: list[CollectedSourceEvent] = []
        for component in components:
            units = (
                [component]
                if prompt_tokens(component) <= input_limit
                else [[item] for item in component]
            )
            for unit in units:
                candidate = [*current, *unit]
                if current and prompt_tokens(candidate) > input_limit:
                    batches.append(current)
                    current = []
                current.extend(unit)
        if current:
            batches.append(current)
        return batches or [list(source_events)]

    def _invoke_collected_review_with_retry(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        candidate_group: CollectedGroupingGroup,
        *,
        reasons: list[str],
    ) -> tuple[CollectedGroupingResult, list[str]]:
        fitted_events, fit_warnings = self._fit_collected_review_events_to_limit(
            target_date,
            source_events,
            candidate_group,
            reasons=reasons,
        )
        warnings = list(fit_warnings)
        retryable_error_count = 0
        invalid_result_count = 0
        attempt_index = 0
        retry_reason = "initial"
        while True:
            attempt_index += 1
            prompt = build_collected_review_prompt(
                target_date,
                fitted_events,
                candidate_group,
                config=self.config,
                review_reasons=reasons,
            )
            prompt_tokens = _estimate_prepared_model_prompt_tokens(prompt)
            if prompt_tokens > self.config.max_model_input_tokens:
                raise ValueError(
                    "Collected review prompt exceeds max_model_input_tokens: "
                    f"estimated_tokens={prompt_tokens} "
                    f"limit={self.config.max_model_input_tokens}"
                )
            trace_step_index = self._start_collected_merge_trace_attempt(
                target_date=target_date,
                source_events=fitted_events,
                deterministic_groups=[list(candidate_group.draft_ids)],
                rolling_step_index=1,
                attempt_index=attempt_index,
                retry_reason=retry_reason,
                stage="high_risk_review",
                prompt_override=prompt,
                extra_trace={
                    "candidate_group": candidate_group.to_dict(),
                    "review_reasons": list(reasons),
                },
            )
            try:
                result = self.analyzer.review_collected_group(
                    target_date,
                    fitted_events,
                    candidate_group,
                    review_reasons=reasons,
                )
            except RetryableAnalyzerProtocolError as exc:
                self._record_collected_merge_trace_failure(
                    step_index=trace_step_index,
                    error=exc,
                    retryable=True,
                )
                if (
                    retryable_error_count
                    >= self.config.collected_merge_retryable_error_limit
                ):
                    raise
                retryable_error_count += 1
                warning = (
                    "Retrying high-risk collected group review after retryable "
                    f"analyzer error: group={candidate_group.group_id} "
                    f"attempt={attempt_index} error={exc}"
                )
                warnings.append(warning)
                self._collected_merge_failure_warnings.append(warning)
                self.sleep_func(self.config.collected_merge_retry_delay_seconds)
                retry_reason = "retryable_error"
                continue
            except (AnalyzerProtocolError, ValueError) as exc:
                self._record_collected_merge_trace_failure(
                    step_index=trace_step_index,
                    error=exc,
                    retryable=False,
                )
                raise

            partition_error = collected_grouping_partition_error(
                result,
                candidate_group.draft_ids,
            )
            self._record_collected_grouping_trace_success(
                step_index=trace_step_index,
                grouping_result=result,
                source_coverage_error=partition_error,
            )
            if not partition_error:
                repaired, repair_warnings = repair_collected_grouping_result(
                    result,
                    source_events,
                    [],
                )
                return repaired, [*warnings, *repair_warnings]
            if (
                invalid_result_count
                >= self.config.collected_merge_missing_field_retry_limit
            ):
                raise AnalyzerProtocolError(
                    "High-risk collected group review did not preserve source "
                    f"coverage: group={candidate_group.group_id} {partition_error}"
                )
            invalid_result_count += 1
            warning = (
                "Retrying high-risk collected group review because source coverage "
                f"was invalid: group={candidate_group.group_id} {partition_error}"
            )
            warnings.append(warning)
            self._collected_merge_failure_warnings.append(warning)
            retry_reason = "invalid_source_coverage"

    def _fit_collected_review_events_to_limit(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        candidate_group: CollectedGroupingGroup,
        *,
        reasons: list[str],
    ) -> tuple[list[CollectedSourceEvent], list[str]]:
        input_limit = self.config.max_model_input_tokens

        def estimate(events: list[CollectedSourceEvent]) -> int:
            return _estimate_prepared_model_prompt_tokens(
                build_collected_review_prompt(
                    target_date,
                    events,
                    candidate_group,
                    config=self.config,
                    review_reasons=reasons,
                )
            )

        full_tokens = estimate(source_events)
        if full_tokens <= input_limit:
            return source_events, []
        empty_events = [
            replace(
                item,
                event=replace(item.event, content=""),
                prompt_original_content_chars=len(clean_text(item.event.content)),
            )
            for item in source_events
        ]
        fixed_tokens = estimate(empty_events)
        if fixed_tokens > input_limit:
            raise ValueError(
                "Collected review fixed fields exceed max_model_input_tokens: "
                f"estimated_tokens={fixed_tokens} limit={input_limit} "
                f"group={candidate_group.group_id}"
            )
        source_totals: dict[str, int] = defaultdict(int)
        for item in source_events:
            source_totals[item.source_file or item.person_name or item.draft_id] += len(
                clean_text(item.event.content)
            )
        low = 0
        high = max(source_totals.values(), default=0)
        best_events = empty_events
        best_tokens = fixed_tokens
        while low <= high:
            quota = (low + high) // 2
            candidate = _apply_balanced_source_content_quota(
                source_events,
                per_source_char_quota=quota,
            )
            candidate_tokens = estimate(candidate)
            if candidate_tokens <= input_limit:
                best_events = candidate
                best_tokens = candidate_tokens
                low = quota + 1
            else:
                high = quota - 1
        self._collected_quality_counters["shortened_prompt_count"] += 1
        return best_events, [
            "Used balanced high-risk review content to fit model input: "
            f"group={candidate_group.group_id} estimated_tokens={best_tokens} "
            f"limit={input_limit}."
        ]

    def _render_collected_multi_groups(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        groups: list[CollectedGroupingGroup],
    ) -> tuple[list[CollectedMergeGroup], list[str]]:
        input_limit_tokens = self.config.max_model_input_tokens
        batches: list[list[CollectedGroupingGroup]] = []
        current: list[CollectedGroupingGroup] = []

        for group in groups:
            candidate_groups = [*current, group]
            candidate_ids = {
                draft_id
                for candidate_group in candidate_groups
                for draft_id in candidate_group.draft_ids
            }
            candidate_events = [
                item for item in source_events if item.draft_id in candidate_ids
            ]
            candidate_tokens = self._estimate_collected_render_prompt_tokens(
                target_date,
                candidate_events,
                [list(item.draft_ids) for item in candidate_groups],
            )
            if current and candidate_tokens > input_limit_tokens:
                batches.append(current)
                current = []
            current.append(group)
        if current:
            batches.append(current)

        rendered_groups: list[CollectedMergeGroup] = []
        warnings: list[str] = []
        for batch_index, batch_groups in enumerate(batches, start=1):
            batch_ids = {
                draft_id
                for group in batch_groups
                for draft_id in group.draft_ids
            }
            batch_events = [
                item for item in source_events if item.draft_id in batch_ids
            ]
            batch_locked = [list(group.draft_ids) for group in batch_groups]
            batch_tokens = self._estimate_collected_render_prompt_tokens(
                target_date,
                batch_events,
                batch_locked,
            )
            if len(batch_groups) == 1 and batch_tokens > input_limit_tokens:
                rendered_group, group_warnings = self._render_oversized_locked_group(
                    target_date,
                    batch_events,
                    batch_groups[0],
                    depth=0,
                )
                rendered_groups.append(rendered_group)
                warnings.extend(group_warnings)
                continue
            result, retry_warnings = self._invoke_collected_merge_with_retry(
                target_date,
                batch_events,
                batch_locked,
                rolling_step_index=batch_index,
            )
            result, repair_warnings = repair_collected_merge_result(
                result,
                batch_events,
                batch_locked,
            )
            rendered_groups.extend(result.groups)
            warnings.extend([*retry_warnings, *repair_warnings])
        if len(batches) > 1:
            warnings.insert(
                0,
                "Using locked-group collected content batches: "
                f"batches={len(batches)} input_limit_tokens={input_limit_tokens}.",
            )
        return rendered_groups, warnings

    def _render_oversized_locked_group(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        group: CollectedGroupingGroup,
        *,
        depth: int,
    ) -> tuple[CollectedMergeGroup, list[str]]:
        input_limit_tokens = self.config.max_model_input_tokens
        locked_group = [item.draft_id for item in source_events]
        prompt_tokens = self._estimate_collected_render_prompt_tokens(
            target_date,
            source_events,
            [locked_group],
        )
        if prompt_tokens <= input_limit_tokens:
            result, retry_warnings = self._invoke_collected_merge_with_retry(
                target_date,
                source_events,
                [locked_group],
                rolling_step_index=depth + 1,
            )
            result, repair_warnings = repair_collected_merge_result(
                result,
                source_events,
                [locked_group],
            )
            rendered = result.groups[0]
            return (
                replace(
                    rendered,
                    draft_ids=list(group.draft_ids),
                    covered_draft_ids=list(group.draft_ids),
                    fact_items=list(rendered.fact_items),
                ),
                [*retry_warnings, *repair_warnings],
            )

        self._collected_quality_counters["shortened_prompt_count"] += 1
        expanded_events: list[CollectedSourceEvent] = []
        original_id_by_expanded_id: dict[str, str] = {}
        split_warnings: list[str] = []
        for item in source_events:
            single_tokens = self._estimate_collected_render_prompt_tokens(
                target_date,
                [item],
                [[item.draft_id]],
            )
            if single_tokens <= input_limit_tokens:
                expanded_events.append(item)
                original_id_by_expanded_id[item.draft_id] = item.draft_id
                continue
            shards = self._split_collected_source_event_for_render(
                target_date,
                item,
                depth=depth,
            )
            expanded_events.extend(shards)
            original_id_by_expanded_id.update(
                {shard.draft_id: item.draft_id for shard in shards}
            )
            split_warnings.append(
                "Split oversized collected source event content: "
                f"draft_id={item.draft_id} shards={len(shards)} "
                f"input_limit_tokens={input_limit_tokens}."
            )
        source_events = expanded_events
        locked_group = [item.draft_id for item in source_events]
        expanded_prompt_tokens = self._estimate_collected_render_prompt_tokens(
            target_date,
            source_events,
            [locked_group],
        )

        chunks: list[list[CollectedSourceEvent]] = []
        current: list[CollectedSourceEvent] = []
        for item in source_events:
            candidate = [*current, item]
            candidate_tokens = self._estimate_collected_render_prompt_tokens(
                target_date,
                candidate,
                [[event.draft_id for event in candidate]],
            )
            if current and candidate_tokens > input_limit_tokens:
                chunks.append(current)
                current = []
            current.append(item)
        if current:
            chunks.append(current)

        summary_events: list[CollectedSourceEvent] = []
        original_ids_by_summary_id: dict[str, list[str]] = {}
        warnings = [
            *split_warnings,
            "Using hierarchical collected content rendering: "
            f"group={group.group_id} depth={depth + 1} chunks={len(chunks)}."
        ]
        for chunk_index, chunk in enumerate(chunks, start=1):
            chunk_ids = [item.draft_id for item in chunk]
            result, retry_warnings = self._invoke_collected_merge_with_retry(
                target_date,
                chunk,
                [chunk_ids],
                rolling_step_index=depth + 1,
            )
            result, repair_warnings = repair_collected_merge_result(
                result,
                chunk,
                [chunk_ids],
            )
            event = self._materialize_events(target_date, chunk, result)[0]
            summary_id = f"__content_summary_{depth}_{chunk_index}"
            original_ids_by_summary_id[summary_id] = _dedupe(
                [
                    original_id_by_expanded_id.get(item.draft_id, item.draft_id)
                    for item in chunk
                ]
            )
            summary_events.append(
                CollectedSourceEvent(
                    draft_id=summary_id,
                    person_name="content-summary",
                    source_file=f"__content_summary_{depth}.md",
                    event=event,
                    is_merge_owner_source=any(
                        item.is_merge_owner_source for item in chunk
                    ),
                )
            )
            warnings.extend([*retry_warnings, *repair_warnings])

        summary_prompt_tokens = self._estimate_collected_render_prompt_tokens(
            target_date,
            summary_events,
            [[item.draft_id for item in summary_events]],
        )
        if (
            summary_prompt_tokens > input_limit_tokens
            and summary_prompt_tokens >= expanded_prompt_tokens
        ):
            raise ValueError(
                "Hierarchical collected content rendering did not reduce model input: "
                f"group={group.group_id} depth={depth + 1} "
                f"before_tokens={expanded_prompt_tokens} "
                f"after_tokens={summary_prompt_tokens} "
                f"limit={input_limit_tokens}"
            )

        rendered, recursive_warnings = self._render_oversized_locked_group(
            target_date,
            summary_events,
            CollectedGroupingGroup(
                group_id=group.group_id,
                draft_ids=[item.draft_id for item in summary_events],
            ),
            depth=depth + 1,
        )
        warnings.extend(recursive_warnings)
        return (
            replace(
                rendered,
                draft_ids=list(group.draft_ids),
                covered_draft_ids=list(group.draft_ids),
                fact_items=[
                    replace(
                        fact,
                        source_draft_ids=_dedupe(
                            [
                                original_id
                                for summary_id in fact.source_draft_ids
                                for original_id in original_ids_by_summary_id.get(
                                    summary_id,
                                    [],
                                )
                            ]
                        ),
                    )
                    for fact in rendered.fact_items
                ],
            ),
            warnings,
        )

    def _split_collected_source_event_for_render(
        self,
        target_date: str,
        source_event: CollectedSourceEvent,
        *,
        depth: int,
    ) -> list[CollectedSourceEvent]:
        input_limit_tokens = self.config.max_model_input_tokens
        original_content = clean_text(source_event.event.content)
        empty_event = replace(
            source_event,
            event=replace(source_event.event, content=""),
            prompt_original_content_chars=len(original_content),
        )
        empty_tokens = self._estimate_collected_render_prompt_tokens(
            target_date,
            [empty_event],
            [[empty_event.draft_id]],
        )
        if empty_tokens > input_limit_tokens:
            raise ValueError(
                "Collected source event fixed fields exceed max_model_input_tokens: "
                f"draft_id={source_event.draft_id} "
                f"estimated_tokens={empty_tokens} limit={input_limit_tokens}"
            )
        if not original_content:
            raise ValueError(
                "Collected source event exceeds max_model_input_tokens without "
                f"splittable content: draft_id={source_event.draft_id}"
            )

        shards: list[CollectedSourceEvent] = []
        remaining = original_content
        part_index = 1
        while remaining:
            low = 1
            high = len(remaining)
            best_length = 0
            while low <= high:
                length = (low + high) // 2
                probe = self._build_collected_content_shard(
                    target_date,
                    source_event,
                    remaining[:length],
                    depth=depth,
                    part_index=part_index,
                    original_content_chars=len(original_content),
                )
                probe_tokens = self._estimate_collected_render_prompt_tokens(
                    target_date,
                    [probe],
                    [[probe.draft_id]],
                )
                if probe_tokens <= input_limit_tokens:
                    best_length = length
                    low = length + 1
                else:
                    high = length - 1
            if best_length <= 0:
                raise ValueError(
                    "Unable to split collected source event below model input limit: "
                    f"draft_id={source_event.draft_id} limit={input_limit_tokens}"
                )
            split_length = _preferred_content_split_index(remaining, best_length)
            content = remaining[:split_length].strip()
            if not content:
                split_length = best_length
                content = remaining[:split_length]
            shards.append(
                self._build_collected_content_shard(
                    target_date,
                    source_event,
                    content,
                    depth=depth,
                    part_index=part_index,
                    original_content_chars=len(original_content),
                )
            )
            remaining = remaining[split_length:].lstrip()
            part_index += 1
        return shards

    def _build_collected_content_shard(
        self,
        target_date: str,
        source_event: CollectedSourceEvent,
        content: str,
        *,
        depth: int,
        part_index: int,
        original_content_chars: int,
    ) -> CollectedSourceEvent:
        draft_id = (
            f"{source_event.draft_id}::__content_part_{depth}_{part_index}"
        )
        source_ids = list(
            source_event.event.source_event_ids
            or [source_event.event.event_id]
        )
        return replace(
            source_event,
            draft_id=draft_id,
            event=replace(
                source_event.event,
                date=target_date,
                event_id=stable_event_id(
                    target_date,
                    [source_event.draft_id, str(depth), str(part_index)],
                    "content-shard",
                ),
                content=content,
                source_event_ids=source_ids,
            ),
            prompt_original_content_chars=original_content_chars,
        )

    def _merge_source_events_single_stage(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        *,
        merge_owner_person: str,
    ) -> tuple[list[WorkEvent], list[str]]:
        deterministic_groups, deterministic_warnings = self._build_deterministic_groups(
            source_events,
        )
        prompt_tokens = self._estimate_collected_merge_prompt_tokens(
            target_date,
            source_events,
            deterministic_groups,
        )
        source_groups = self._group_source_events_for_rolling(
            target_date,
            source_events,
        )
        input_limit_tokens = self.config.max_model_input_tokens
        if prompt_tokens <= input_limit_tokens or len(source_groups) < 3:
            merged_events, merge_warnings = self._merge_collected_event_batch(
                target_date,
                source_events,
                deterministic_groups=deterministic_groups,
                rolling_step_index=1,
            )
            return merged_events, [*deterministic_warnings, *merge_warnings]

        merged_events, rolling_warnings, call_count = self._merge_source_events_rolling(
            target_date,
            source_groups,
            merge_owner_person=merge_owner_person,
        )
        rolling_notice = (
            "Using rolling collected merge: "
            f"prompt_estimated_tokens={prompt_tokens} "
            f"input_limit_tokens={input_limit_tokens} calls={call_count}"
        )
        return merged_events, [*deterministic_warnings, rolling_notice, *rolling_warnings]

    def _merge_source_events_rolling(
        self,
        target_date: str,
        source_groups: list[list[CollectedSourceEvent]],
        *,
        merge_owner_person: str,
    ) -> tuple[list[WorkEvent], list[str], int]:
        current_events = source_groups[0]
        warnings: list[str] = []
        call_count = 0

        for step_index, next_events in enumerate(source_groups[1:], start=1):
            batch_events = [*current_events, *next_events]
            deterministic_groups, deterministic_warnings = (
                self._build_deterministic_groups(batch_events)
            )
            warnings.extend(deterministic_warnings)
            merged_events, merge_warnings = self._merge_collected_event_batch(
                target_date,
                batch_events,
                deterministic_groups=deterministic_groups,
                rolling_step_index=step_index,
            )
            call_count += 1
            warnings.extend(merge_warnings)
            current_events = build_synthetic_collected_source_events(
                target_date,
                merged_events,
                step_index=step_index,
                merge_owner_person=merge_owner_person,
            )

        return [item.event for item in current_events], warnings, call_count

    def _merge_collected_event_batch(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        *,
        deterministic_groups: list[list[str]] | None = None,
        rolling_step_index: int = 1,
    ) -> tuple[list[WorkEvent], list[str]]:
        deterministic_groups = (
            deterministic_groups
            if deterministic_groups is not None
            else self._build_deterministic_groups(source_events)[0]
        )
        merge_result, retry_warnings = self._invoke_collected_merge_with_retry(
            target_date,
            source_events,
            deterministic_groups,
            rolling_step_index=rolling_step_index,
        )
        merge_result, repair_warnings = repair_collected_merge_result(
            merge_result,
            source_events,
            deterministic_groups,
        )
        merged_events, final_warnings = self._finalize_collected_merge_result(
            target_date,
            source_events,
            merge_result,
            repair_warnings=repair_warnings,
        )
        return merged_events, [*retry_warnings, *final_warnings]

    def _finalize_collected_merge_result(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        merge_result: CollectedMergeResult,
        *,
        repair_warnings: list[str] | None = None,
    ) -> tuple[list[WorkEvent], list[str]]:
        repair_warnings = list(repair_warnings or [])
        merge_result, boundary_warnings = enforce_collected_workstream_boundaries(
            merge_result,
            source_events,
        )
        merge_result, metadata_warnings = self._fill_collected_merge_group_metadata(
            source_events,
            merge_result,
        )
        merged_events_before_filters = self._materialize_events(
            target_date,
            source_events,
            merge_result,
        )
        (
            merged_events_after_sensitive,
            sensitive_warnings,
            merged_filter_diagnostics,
        ) = self._filter_events(
            merged_events_before_filters,
            source_events=source_events,
        )
        self._collected_merge_filter_diagnostics.extend(merged_filter_diagnostics)
        retained_events, retention_warnings = filter_retained_work_events(
            merged_events_after_sensitive,
        )
        self._record_collected_merge_trace_final(
            repaired_result=merge_result,
            merged_events_before_filters=merged_events_before_filters,
            merged_events_after_sensitive=merged_events_after_sensitive,
            retained_events=retained_events,
            repair_warnings=repair_warnings,
            boundary_warnings=boundary_warnings,
            metadata_warnings=metadata_warnings,
            sensitive_warnings=sensitive_warnings,
            retention_warnings=retention_warnings,
        )
        return retained_events, [
            *repair_warnings,
            *boundary_warnings,
            *metadata_warnings,
            *sensitive_warnings,
            *retention_warnings,
        ]

    def _invoke_collected_grouping_with_retry(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        deterministic_groups: list[list[str]],
    ) -> tuple[CollectedGroupingResult, list[str]]:
        return self._group_collected_events_with_batching(
            target_date,
            source_events,
            deterministic_groups,
            depth=0,
        )

    def _group_collected_events_with_batching(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        deterministic_groups: list[list[str]],
        *,
        depth: int,
    ) -> tuple[CollectedGroupingResult, list[str]]:
        prompt_tokens = self._estimate_collected_grouping_prompt_tokens(
            target_date,
            source_events,
            deterministic_groups,
        )
        input_limit_tokens = self.config.max_model_input_tokens
        if prompt_tokens <= input_limit_tokens:
            return self._invoke_collected_grouping_once(
                target_date,
                source_events,
                deterministic_groups,
                stage=(
                    "candidate_grouping"
                    if depth == 0
                    else "candidate_reconciliation"
                ),
            )

        batches = self._pack_collected_grouping_batches(
            target_date,
            source_events,
            deterministic_groups,
        )
        warnings = [
            "Using relation-priority collected candidate grouping: "
            f"prompt_estimated_tokens={prompt_tokens} "
            f"input_limit_tokens={input_limit_tokens} batches={len(batches)}."
        ]
        partial_groups: list[CollectedGroupingGroup] = []
        deterministic_sets = {tuple(group) for group in deterministic_groups}
        for batch_index, batch_events in enumerate(batches, start=1):
            batch_ids = {item.draft_id for item in batch_events}
            batch_deterministic = [
                list(group)
                for group in deterministic_groups
                if set(group).issubset(batch_ids)
            ]
            fitted_batch_events, fit_warnings = (
                self._fit_collected_grouping_events_to_limit(
                    target_date,
                    batch_events,
                    batch_deterministic,
                )
            )
            batch_result, batch_warnings = self._invoke_collected_grouping_once(
                target_date,
                fitted_batch_events,
                batch_deterministic,
                stage=(
                    f"candidate_grouping_batch_{batch_index}"
                    if depth == 0
                    else f"candidate_reconciliation_batch_{batch_index}"
                ),
            )
            batch_result, batch_repair_warnings = repair_collected_grouping_result(
                batch_result,
                batch_events,
                batch_deterministic,
            )
            partial_groups.extend(batch_result.groups)
            warnings.extend(
                [*fit_warnings, *batch_warnings, *batch_repair_warnings]
            )

        if len(batches) <= 1:
            return CollectedGroupingResult(groups=partial_groups), warnings
        if depth >= 3:
            warnings.append(
                "Stopped collected candidate reconciliation after 3 levels; "
                "all batch groups were preserved without cross-batch merging."
            )
            return CollectedGroupingResult(groups=partial_groups), warnings

        synthetic_events, source_ids_by_synthetic = build_grouping_summary_events(
            target_date,
            source_events,
            partial_groups,
            depth=depth + 1,
        )
        synthetic_deterministic = [
            [synthetic_id]
            for synthetic_id, original_ids in source_ids_by_synthetic.items()
            if tuple(original_ids) in deterministic_sets
        ]
        reconciled, reconciliation_warnings = self._group_collected_events_with_batching(
            target_date,
            synthetic_events,
            synthetic_deterministic,
            depth=depth + 1,
        )
        warnings.extend(reconciliation_warnings)
        repaired_original_ids = {
            draft_id
            for partial_group in partial_groups
            if partial_group.was_repaired
            for draft_id in partial_group.draft_ids
        }
        expanded_groups = [
            CollectedGroupingGroup(
                group_id=f"reconciled-{index}",
                draft_ids=_dedupe(
                    [
                        original_id
                        for synthetic_id in group.draft_ids
                        for original_id in source_ids_by_synthetic.get(
                            synthetic_id,
                            [],
                        )
                    ]
                ),
                summary_title=group.summary_title,
                summary_content=group.summary_content,
                summary_object_hint=group.summary_object_hint,
                summary_source=group.summary_source,
                group_reason=list(group.group_reason),
                risk_flags=_dedupe([*group.risk_flags, "cross_batch"]),
                was_repaired=(
                    group.was_repaired
                    or any(
                        original_id in repaired_original_ids
                        for synthetic_id in group.draft_ids
                        for original_id in source_ids_by_synthetic.get(
                            synthetic_id,
                            [],
                        )
                    )
                ),
            )
            for index, group in enumerate(reconciled.groups, start=1)
        ]
        return CollectedGroupingResult(groups=expanded_groups), warnings

    def _pack_collected_grouping_batches(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        deterministic_groups: list[list[str]],
    ) -> list[list[CollectedSourceEvent]]:
        input_limit_tokens = self.config.max_model_input_tokens
        components = build_collected_relation_components(
            source_events,
            deterministic_groups,
        )
        source_by_id = {item.draft_id: item for item in source_events}
        locked_by_member = {
            draft_id: list(group)
            for group in deterministic_groups
            for draft_id in group
        }
        batches: list[list[CollectedSourceEvent]] = []
        current: list[CollectedSourceEvent] = []

        for component in components:
            candidate = [*current, *component]
            candidate_ids = {item.draft_id for item in candidate}
            candidate_deterministic = [
                list(group)
                for group in deterministic_groups
                if set(group).issubset(candidate_ids)
            ]
            candidate_tokens = self._estimate_collected_grouping_prompt_tokens(
                target_date,
                candidate,
                candidate_deterministic,
            )
            if current and candidate_tokens > input_limit_tokens:
                batches.append(current)
                current = []

            component_tokens = self._estimate_collected_grouping_prompt_tokens(
                target_date,
                component,
                [
                    list(group)
                    for group in deterministic_groups
                    if set(group).issubset(
                        {item.draft_id for item in component}
                    )
                ],
            )
            if component_tokens <= input_limit_tokens:
                current.extend(component)
                continue

            atomic_units: list[list[CollectedSourceEvent]] = []
            consumed_locked_ids: set[str] = set()
            component_ids = {item.draft_id for item in component}
            for item in component:
                locked_group = locked_by_member.get(item.draft_id)
                if locked_group:
                    locked_key = locked_group[0]
                    if locked_key in consumed_locked_ids:
                        continue
                    consumed_locked_ids.add(locked_key)
                    atomic_units.append(
                        [
                            source_by_id[draft_id]
                            for draft_id in locked_group
                            if draft_id in component_ids
                        ]
                    )
                    continue
                atomic_units.append([item])

            for unit in atomic_units:
                candidate = [*current, *unit]
                candidate_tokens = self._estimate_collected_grouping_prompt_tokens(
                    target_date,
                    candidate,
                    [],
                )
                if current and candidate_tokens > input_limit_tokens:
                    batches.append(current)
                    current = []
                current.extend(unit)

        if current:
            batches.append(current)
        return batches or [list(source_events)]

    def _fit_collected_grouping_events_to_limit(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        deterministic_groups: list[list[str]],
    ) -> tuple[list[CollectedSourceEvent], list[str]]:
        input_limit_tokens = self.config.max_model_input_tokens
        full_tokens = self._estimate_collected_grouping_prompt_tokens(
            target_date,
            source_events,
            deterministic_groups,
        )
        if full_tokens <= input_limit_tokens:
            return source_events, []

        empty_events = [
            replace(
                item,
                event=replace(item.event, content=""),
                prompt_original_content_chars=len(clean_text(item.event.content)),
            )
            for item in source_events
        ]
        fixed_tokens = self._estimate_collected_grouping_prompt_tokens(
            target_date,
            empty_events,
            deterministic_groups,
        )
        if fixed_tokens > input_limit_tokens:
            locked_ids = [
                draft_id
                for group in deterministic_groups
                for draft_id in group
            ]
            raise ValueError(
                "Collected candidate fixed fields exceed max_model_input_tokens: "
                f"estimated_tokens={fixed_tokens} limit={input_limit_tokens} "
                f"draft_ids={[item.draft_id for item in source_events]} "
                f"deterministic_ids={locked_ids}"
            )

        source_totals: dict[str, int] = defaultdict(int)
        for item in source_events:
            source_key = item.source_file or item.person_name or item.draft_id
            source_totals[source_key] += len(clean_text(item.event.content))
        low = 0
        high = max(source_totals.values(), default=0)
        best_events = empty_events
        best_tokens = fixed_tokens
        while low <= high:
            quota = (low + high) // 2
            candidate = _apply_balanced_source_content_quota(
                source_events,
                per_source_char_quota=quota,
            )
            candidate_tokens = self._estimate_collected_grouping_prompt_tokens(
                target_date,
                candidate,
                deterministic_groups,
            )
            if candidate_tokens <= input_limit_tokens:
                best_events = candidate
                best_tokens = candidate_tokens
                low = quota + 1
            else:
                high = quota - 1

        shortened_sources = sorted(
            {
                item.source_file or item.person_name or item.draft_id
                for original, item in zip(source_events, best_events, strict=True)
                if len(clean_text(item.event.content))
                < len(clean_text(original.event.content))
            }
        )
        warning = (
            "Used balanced collected candidate content to fit model input: "
            f"estimated_tokens={best_tokens} limit={input_limit_tokens} "
            f"shortened_sources={shortened_sources}."
        )
        self._collected_quality_counters["shortened_prompt_count"] += 1
        return best_events, [warning]

    def _invoke_collected_grouping_once(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        deterministic_groups: list[list[str]],
        *,
        stage: str,
    ) -> tuple[CollectedGroupingResult, list[str]]:
        warnings: list[str] = []
        retryable_error_count = 0
        attempt_index = 0
        retry_reason = "initial"
        while True:
            attempt_index += 1
            prompt_tokens = self._estimate_collected_grouping_prompt_tokens(
                target_date,
                source_events,
                deterministic_groups,
            )
            if prompt_tokens > self.config.max_model_input_tokens:
                raise ValueError(
                    "Collected candidate prompt exceeds max_model_input_tokens: "
                    f"estimated_tokens={prompt_tokens} "
                    f"limit={self.config.max_model_input_tokens}"
                )
            trace_step_index = self._start_collected_merge_trace_attempt(
                target_date=target_date,
                source_events=source_events,
                deterministic_groups=deterministic_groups,
                rolling_step_index=0,
                attempt_index=attempt_index,
                retry_reason=retry_reason,
                stage=stage,
            )
            try:
                grouping_result = self.analyzer.group_collected_events(
                    target_date,
                    source_events,
                    deterministic_groups,
                )
            except RetryableAnalyzerProtocolError as exc:
                self._record_collected_merge_trace_failure(
                    step_index=trace_step_index,
                    error=exc,
                    retryable=True,
                )
                if retryable_error_count >= self.config.collected_merge_retryable_error_limit:
                    raise
                retryable_error_count += 1
                warning = (
                    "Retrying collected candidate grouping after retryable analyzer error: "
                    f"attempt={attempt_index} error={exc}"
                )
                warnings.append(warning)
                self._collected_merge_failure_warnings.append(warning)
                self.sleep_func(self.config.collected_merge_retry_delay_seconds)
                retry_reason = "retryable_error"
                continue
            except (AnalyzerProtocolError, ValueError) as exc:
                self._record_collected_merge_trace_failure(
                    step_index=trace_step_index,
                    error=exc,
                    retryable=False,
                )
                raise
            self._record_collected_grouping_trace_success(
                step_index=trace_step_index,
                grouping_result=grouping_result,
            )
            return grouping_result, warnings

    def _invoke_collected_merge_with_retry(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        deterministic_groups: list[list[str]],
        *,
        rolling_step_index: int,
    ) -> tuple[CollectedMergeResult, list[str]]:
        warnings: list[str] = []
        missing_field_retry_count = 0
        retryable_error_count = 0
        attempt_index = 0
        retry_reason = "initial"
        while True:
            attempt_index += 1
            prompt_tokens = self._estimate_collected_render_prompt_tokens(
                target_date,
                source_events,
                deterministic_groups,
            )
            if prompt_tokens > self.config.max_model_input_tokens:
                raise ValueError(
                    "Collected render prompt exceeds max_model_input_tokens: "
                    f"estimated_tokens={prompt_tokens} "
                    f"limit={self.config.max_model_input_tokens}"
                )
            trace_step_index = self._start_collected_merge_trace_attempt(
                target_date=target_date,
                source_events=source_events,
                deterministic_groups=deterministic_groups,
                rolling_step_index=rolling_step_index,
                attempt_index=attempt_index,
                retry_reason=retry_reason,
            )
            try:
                merge_result = self.analyzer.merge_collected_events(
                    target_date,
                    source_events,
                    deterministic_groups,
                )
            except RetryableAnalyzerProtocolError as exc:
                self._record_collected_merge_trace_failure(
                    step_index=trace_step_index,
                    error=exc,
                    retryable=True,
                )
                if retryable_error_count >= self.config.collected_merge_retryable_error_limit:
                    raise
                retryable_error_count += 1
                warning = (
                    "Retrying collected merge after retryable analyzer error: "
                    f"rolling_step={rolling_step_index} "
                    f"attempt={attempt_index} error={exc}"
                )
                warnings.append(warning)
                self._collected_merge_failure_warnings.append(warning)
                self.sleep_func(self.config.collected_merge_retry_delay_seconds)
                retry_reason = "retryable_error"
                continue
            except (AnalyzerProtocolError, ValueError) as exc:
                self._record_collected_merge_trace_failure(
                    step_index=trace_step_index,
                    error=exc,
                    retryable=False,
                )
                raise
            missing_summary = collected_merge_missing_field_summary(merge_result)
            coverage_error = "; ".join(
                value
                for value in (
                    collected_merge_partition_error(
                        merge_result,
                        [item.draft_id for item in source_events],
                        locked_groups=deterministic_groups,
                    ),
                    collected_merge_coverage_error(merge_result),
                )
                if value
            )
            self._record_collected_merge_trace_success(
                step_index=trace_step_index,
                merge_result=merge_result,
                missing_summary=missing_summary,
                coverage_error=coverage_error,
            )
            if coverage_error:
                if (
                    missing_field_retry_count
                    >= self.config.collected_merge_missing_field_retry_limit
                ):
                    raise AnalyzerProtocolError(
                        "Collected content did not preserve source coverage: "
                        f"{coverage_error}"
                    )
                missing_field_retry_count += 1
                self._collected_quality_counters["content_retry_count"] += 1
                warning = (
                    "Retrying collected content because source coverage was "
                    f"invalid: {coverage_error}"
                )
                warnings.append(warning)
                self._collected_merge_failure_warnings.append(warning)
                retry_reason = "invalid_source_coverage"
                continue
            if not self._should_retry_collected_merge_missing_fields(
                merge_result,
                missing_summary,
            ):
                return merge_result, warnings
            if (
                missing_field_retry_count
                >= self.config.collected_merge_missing_field_retry_limit
            ):
                if missing_summary.get("content", 0):
                    raise AnalyzerProtocolError(
                        "Collected content remained empty after retry: "
                        f"{format_collected_merge_missing_field_summary(missing_summary)}"
                    )
                return merge_result, warnings
            missing_field_retry_count += 1
            self._collected_quality_counters["content_retry_count"] += 1
            warning = (
                "Retrying collected merge because required fields were missing: "
                f"{format_collected_merge_missing_field_summary(missing_summary)}"
            )
            warnings.append(warning)
            self._collected_merge_failure_warnings.append(warning)
            retry_reason = "missing_required_fields"

    def _should_retry_collected_merge_missing_fields(
        self,
        merge_result: CollectedMergeResult,
        missing_summary: dict[str, int],
    ) -> bool:
        group_count = len(merge_result.groups)
        if group_count == 0:
            return False
        max_missing_count = max(missing_summary.values(), default=0)
        missing_ratio = max_missing_count / group_count
        return missing_ratio > self.config.collected_merge_missing_field_retry_ratio

    def _fill_collected_merge_group_metadata(
        self,
        source_events: list[CollectedSourceEvent],
        merge_result: CollectedMergeResult,
    ) -> tuple[CollectedMergeResult, list[str]]:
        source_by_id = {item.draft_id: item for item in source_events}
        groups: list[CollectedMergeGroup] = []
        warnings: list[str] = []
        for group in merge_result.groups:
            items = [
                source_by_id[draft_id]
                for draft_id in group.draft_ids
                if draft_id in source_by_id
            ]
            filled_fields: list[str] = []
            title = clean_text(group.title) or choose_preferred_text(
                [item.event.title for item in items]
            )
            if title != group.title:
                filled_fields.append("title")
            content = clean_text(group.content) or choose_preferred_text(
                [item.event.content for item in items]
            )
            if content != group.content:
                filled_fields.append("content")
            has_merge_owner_source = any(item.is_merge_owner_source for item in items)
            merge_owner_conflict = bool(
                group.merge_owner_conflict and has_merge_owner_source
            )
            conflict_detail = clean_text(group.conflict_detail)
            if not merge_owner_conflict:
                conflict_detail = ""
            elif not conflict_detail:
                conflict_detail = "不同来源存在明确事实冲突，已采用合并人来源。"
            object_hint = clean_text(group.object_hint) or choose_preferred_text(
                [item.event.object_hint for item in items]
            )
            if object_hint != group.object_hint:
                filled_fields.append("object_hint")
            retention_reason = clean_text(group.retention_reason)
            if retention_reason not in RETENTION_REASONS:
                retention_reason = choose_preferred_text(
                    [
                        item.event.retention_reason
                        for item in items
                        if item.event.retention_reason in RETENTION_REASONS
                    ]
                )
                filled_fields.append("retention_reason")
            retention_detail = clean_text(group.retention_detail)
            draft_event = WorkEvent(
                date="",
                event_id="",
                title=title,
                content=content,
                object_hint=object_hint,
                retention_reason=retention_reason,
                retention_detail=retention_detail,
            )
            if (
                not retention_detail
                or retention_rejection_reason_for_event(draft_event)
                == "missing_or_generic_retention_detail"
            ):
                derived_detail = derive_collected_merge_retention_detail(items)
                if derived_detail and derived_detail != retention_detail:
                    retention_detail = derived_detail
                    filled_fields.append("retention_detail")

            groups.append(
                CollectedMergeGroup(
                    group_id=group.group_id,
                    draft_ids=list(group.draft_ids),
                    title=title,
                    content=content,
                    object_hint=object_hint,
                    retention_reason=retention_reason,
                    retention_detail=retention_detail,
                    merge_owner_conflict=merge_owner_conflict,
                    conflict_detail=conflict_detail,
                    covered_draft_ids=(
                        None
                        if group.covered_draft_ids is None
                        else list(group.covered_draft_ids)
                    ),
                    fact_items=list(group.fact_items),
                )
            )
            if merge_owner_conflict:
                warnings.append(
                    "Resolved explicit source conflict in favor of merge owner: "
                    f"{title or group.group_id} ({conflict_detail})"
                )
            if filled_fields:
                warning_title = title or group.group_id or "(empty title)"
                warnings.append(
                    "Filled collected merge metadata from source events: "
                    f"{warning_title} ({', '.join(dict.fromkeys(filled_fields))})"
                )
        return CollectedMergeResult(groups=groups), warnings

    def _estimate_collected_merge_prompt_tokens(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        deterministic_groups: list[list[str]],
    ) -> int:
        return _estimate_prepared_model_prompt_tokens(
            build_collected_merge_prompt(
                target_date,
                source_events,
                deterministic_groups,
                config=self.config,
            )
        )

    def _estimate_collected_render_prompt_tokens(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        deterministic_groups: list[list[str]],
    ) -> int:
        return _estimate_prepared_model_prompt_tokens(
            build_collected_render_prompt(
                target_date,
                source_events,
                deterministic_groups,
                config=self.config,
            )
        )

    def _estimate_collected_grouping_prompt_tokens(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        deterministic_groups: list[list[str]],
    ) -> int:
        return _estimate_prepared_model_prompt_tokens(
            build_collected_grouping_prompt(
                target_date,
                source_events,
                deterministic_groups,
                config=self.config,
            )
        )

    def _group_source_events_for_rolling(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
    ) -> list[list[CollectedSourceEvent]]:
        grouped: dict[str, list[CollectedSourceEvent]] = defaultdict(list)
        for source_event in source_events:
            grouped[source_event.source_file].append(source_event)

        return sorted(
            grouped.values(),
            key=lambda items: (
                self._estimate_collected_merge_prompt_tokens(
                    target_date,
                    items,
                    self._build_deterministic_groups(items)[0],
                ),
                items[0].source_file if items else "",
            ),
        )

    def build_input_dir(self, target_date: str) -> Path:
        year, month, day = target_date.split("-")
        return self.cwd / "merge_inbox" / year / month / day

    def _read_source_events(
        self,
        target_date: str,
        input_dir: Path,
        *,
        output_path: Path,
        ignored_subdirectories: set[str] | None = None,
    ) -> tuple[
        list[CollectedSourceEvent],
        int,
        int,
        int,
        list[str],
        list[dict[str, Any]],
    ]:
        warnings: list[str] = []
        source_audit: list[dict[str, Any]] = []
        source_events: list[CollectedSourceEvent] = []
        source_file_count = 0
        skipped_file_count = 0
        partial_file_count = 0
        ignored_subdirectories = ignored_subdirectories or set()

        if not input_dir.exists():
            return (
                [],
                0,
                0,
                0,
                [f"Input directory does not exist: {input_dir}"],
                [],
            )

        for path in sorted(input_dir.iterdir()):
            if path.is_dir() and not path.name.startswith("."):
                if path.name not in ignored_subdirectories:
                    skipped_file_count += 1
                    warnings.append(f"Skipped nested input directory: {path.name}")
                continue
            if should_skip_input_file(path, output_filename=output_path.name):
                continue
            if path.suffix != ".md":
                skipped_file_count += 1
                continue
            source_file_count += 1
            person_name = extract_source_name_from_filename(
                path.name,
                target_date=target_date,
            )
            parsed_filename = parse_worktrace_markdown_filename(path.name)
            if not person_name:
                skipped_file_count += 1
                warnings.append(f"Skipped invalid source filename: {path.name}")
                source_audit.append(
                    {
                        "source_file": path.name,
                        "person_name": "",
                        "format": "unknown",
                        "status": "skipped",
                        "declared_event_count": None,
                        "parsed_event_count": 0,
                        "partial_event_ids": [],
                        "partial_reason": "",
                        "warning_messages": ["Invalid source filename."],
                    }
                )
                continue
            try:
                markdown_text = path.read_text(encoding="utf-8")
                day_doc = self.store.parse_day_document(
                    markdown_text,
                    allow_trailing_partial=True,
                )
            except (OSError, StoreWriteError, KeyError, ValueError) as exc:
                skipped_file_count += 1
                warnings.append(f"Skipped invalid source markdown: {path.name} ({exc})")
                source_audit.append(
                    {
                        "source_file": path.name,
                        "person_name": person_name,
                        "format": _classify_source_markdown(path.name, []),
                        "status": "skipped",
                        "declared_event_count": self.store.last_declared_event_count,
                        "parsed_event_count": 0,
                        "partial_event_ids": [],
                        "partial_reason": "",
                        "warning_messages": [str(exc)],
                    }
                )
                continue
            partial_event_ids = list(self.store.last_partial_event_ids)
            parser_warnings = [
                warning
                for warning in self.store.last_warning_messages
                if not warning.startswith("Skipped malformed trailing event block:")
            ]
            if partial_event_ids:
                partial_file_count += 1
                partial_warning = (
                    f"Partially read source markdown: {path.name} "
                    f"(declared={self.store.last_declared_event_count} "
                    f"parsed={len(day_doc.events)} "
                    f"skipped_event_ids={','.join(partial_event_ids)} "
                    "reason=malformed trailing event block)."
                )
                warnings.append(partial_warning)
            warnings.extend(
                f"{path.name}: {warning}"
                for warning in parser_warnings
            )
            if day_doc.date != target_date:
                warnings.append(
                    f"Source markdown date mismatch: {path.name} ({day_doc.date})"
                )
            if (
                self.store.last_declared_event_count is not None
                and self.store.last_declared_event_count != len(day_doc.events)
                and not partial_event_ids
            ):
                warnings.append(
                    f"Source markdown event count mismatch: {path.name} "
                    f"(declared={self.store.last_declared_event_count} "
                    f"parsed={len(day_doc.events)})."
                )
            source_audit.append(
                {
                    "source_file": path.name,
                    "person_name": person_name,
                    "format": _classify_source_markdown(path.name, day_doc.events),
                    "status": "partial" if partial_event_ids else "success",
                    "declared_event_count": self.store.last_declared_event_count,
                    "parsed_event_count": len(day_doc.events),
                    "partial_event_ids": partial_event_ids,
                    "partial_reason": (
                        "malformed trailing event block" if partial_event_ids else ""
                    ),
                    "warning_messages": parser_warnings,
                }
            )
            for index, event in enumerate(day_doc.events, start=1):
                source_events.append(
                    CollectedSourceEvent(
                        draft_id=build_collected_draft_id(path.name, index, event.event_id),
                        person_name=person_name,
                        source_file=path.name,
                        event=event,
                        source_report_owner=(
                            person_name if parsed_filename.is_merged else ""
                        ),
                    )
                )

        return (
            source_events,
            source_file_count,
            skipped_file_count,
            partial_file_count,
            warnings,
            source_audit,
        )

    def _build_deterministic_groups(
        self,
        source_events: list[CollectedSourceEvent],
    ) -> tuple[list[list[str]], list[str]]:
        grouped: dict[str, list[CollectedSourceEvent]] = defaultdict(list)
        for source_event in source_events:
            event_id = source_event.event.event_id.strip()
            if event_id:
                grouped[event_id].append(source_event)

        deterministic_groups: list[list[str]] = []
        warnings: list[str] = []
        for event_id, items in grouped.items():
            if len(items) <= 1:
                continue
            if collected_events_are_similar(items):
                deterministic_groups.append([item.draft_id for item in items])
                continue
            warnings.append(
                f"Same event_id has divergent content: {event_id}"
            )
        return deterministic_groups, warnings

    def _filter_retained_source_events(
        self,
        source_events: list[CollectedSourceEvent],
    ) -> tuple[list[CollectedSourceEvent], list[str]]:
        events = [item.event for item in source_events]
        kept_events, warnings = filter_retained_work_events(events)
        kept_ids = {id(event) for event in kept_events}
        return [
            source_event
            for source_event in source_events
            if id(source_event.event) in kept_ids
        ], warnings

    def _filter_source_events(
        self,
        source_events: list[CollectedSourceEvent],
    ) -> tuple[list[CollectedSourceEvent], list[str], list[dict[str, Any]]]:
        kept_events, raw_diagnostics = filter_work_events_with_diagnostics(
            [item.event for item in source_events],
            self.config,
        )
        kept_ids = {id(event) for event in kept_events}
        kept_source_events = [
            source_event
            for source_event in source_events
            if id(source_event.event) in kept_ids
        ]
        diagnostics: list[dict[str, Any]] = []
        warnings: list[str] = []
        for raw in raw_diagnostics:
            source_event = source_events[raw.item_index]
            diagnostic = {
                "stage": "source_filter",
                "kind": raw.kind,
                "source_file": source_event.source_file,
                "source_person": source_event.person_name,
                "event_id": source_event.event.event_id,
                "event_title": source_event.event.title,
            }
            diagnostics.append(diagnostic)
            warnings.append(_render_filter_warning(diagnostic))
        return kept_source_events, warnings, diagnostics

    def _build_source_retention_diagnostics(
        self,
        before_events: list[CollectedSourceEvent],
        after_events: list[CollectedSourceEvent],
    ) -> list[dict[str, Any]]:
        kept_ids = {id(item.event) for item in after_events}
        return [
            {
                "stage": "source_retention_filter",
                "kind": "retention",
                "source_file": item.source_file,
                "source_person": item.person_name,
                "event_id": item.event.event_id,
                "event_title": item.event.title,
                "rejection_reason": retention_rejection_reason_for_event(item.event),
            }
            for item in before_events
            if id(item.event) not in kept_ids
        ]

    def _finalize_source_audit(
        self,
        source_audit: list[dict[str, Any]],
        *,
        parsed_source_events: list[CollectedSourceEvent],
        model_input_events: list[CollectedSourceEvent],
        filter_diagnostics: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        parsed_counts = Counter(item.source_file for item in parsed_source_events)
        input_counts = Counter(item.source_file for item in model_input_events)
        filter_counts = Counter(
            (str(item.get("source_file", "")), str(item.get("kind", "")))
            for item in filter_diagnostics
            if item.get("stage") in {"source_filter", "source_retention_filter"}
        )
        finalized: list[dict[str, Any]] = []
        for item in source_audit:
            source_file = str(item.get("source_file", ""))
            finalized.append(
                {
                    **item,
                    "parsed_event_count": parsed_counts.get(
                        source_file,
                        int(item.get("parsed_event_count", 0)),
                    ),
                    "sensitive_filtered_count": filter_counts.get(
                        (source_file, "sensitive"),
                        0,
                    ),
                    "excluded_filtered_count": filter_counts.get(
                        (source_file, "excluded"),
                        0,
                    ),
                    "retention_filtered_count": filter_counts.get(
                        (source_file, "retention"),
                        0,
                    ),
                    "model_input_event_count": input_counts.get(source_file, 0),
                }
            )
        return finalized

    def _mark_merge_owner_sources(
        self,
        source_events: list[CollectedSourceEvent],
        *,
        merge_owner_person: str,
        input_dir: Path,
    ) -> tuple[list[CollectedSourceEvent], list[str]]:
        owner_name = merge_owner_person.strip()
        if not source_events or not owner_name:
            return source_events, []

        marked_events = [
            replace(
                source_event,
                is_merge_owner_source=source_event.person_name.strip() == owner_name,
            )
            for source_event in source_events
        ]
        if any(event.is_merge_owner_source for event in marked_events):
            return marked_events, []
        return marked_events, []

    def _materialize_events(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        merge_result: CollectedMergeResult,
    ) -> list[WorkEvent]:
        source_by_id = {item.draft_id: item for item in source_events}
        events: list[WorkEvent] = []
        seen_event_ids: set[str] = set()
        for group in merge_result.groups:
            items = [
                source_by_id[draft_id]
                for draft_id in group.draft_ids
                if draft_id in source_by_id
            ]
            if not items:
                continue
            source_people = _dedupe(
                [
                    person_name
                    for item in items
                    for person_name in (
                        item.event.source_people or [item.person_name]
                    )
                ]
            )
            source_event_ids = _dedupe(
                [
                    source_event_id
                    for item in items
                    for source_event_id in (
                        item.event.source_event_ids or [item.event.event_id]
                    )
                ]
            )
            source_report_owners = _dedupe(
                [
                    owner
                    for item in items
                    for owner in [
                        *item.event.source_report_owners,
                        item.source_report_owner,
                    ]
                    if owner
                ]
            )
            file_links = _merge_file_links(items)
            workstream_names = _dedupe(
                [item.event.workstream_name for item in items]
            )
            normalized_workstream_names = {
                "".join(name.casefold().split()) for name in workstream_names
            }
            output_workstream_name = (
                workstream_names[0]
                if len(normalized_workstream_names) <= 1 and workstream_names
                else ""
            )
            action_labels = _dedupe(
                [
                    label
                    for item in items
                    for label in item.event.action_labels
                ]
            )
            self_relations = _sort_self_relations(
                [
                    relation
                    for item in items
                    for relation in item.event.self_relations
                ],
                config=self.config,
            )
            evidence_fingerprints = _dedupe(
                [
                    value
                    for item in items
                    for value in item.event.evidence_fingerprints
                ]
            )
            conversation_fingerprints = _dedupe(
                [
                    value
                    for item in items
                    for value in item.event.conversation_fingerprints
                ]
            )
            file_keys = _dedupe(
                [
                    *(value for item in items for value in item.event.file_keys),
                    *(
                        key
                        for link in file_links
                        if (key := file_key_from_url(link.url))
                    ),
                ]
            )
            event_id = stable_event_id(
                target_date,
                group.draft_ids,
                "\n".join([group.title, group.content]),
            )
            if event_id in seen_event_ids:
                raise ValueError(f"Unresolvable collected event_id collision: {event_id}")
            seen_event_ids.add(event_id)
            events.append(
                WorkEvent(
                    date=target_date,
                    event_id=event_id,
                    title=group.title,
                    content=group.content,
                    file_links=file_links,
                    source_people=source_people,
                    source_event_ids=source_event_ids,
                    source_report_owners=source_report_owners,
                    object_hint=group.object_hint,
                    retention_reason=group.retention_reason,
                    retention_detail=group.retention_detail,
                    workstream_name=output_workstream_name,
                    action_labels=action_labels,
                    self_relations=self_relations,
                    evidence_fingerprints=evidence_fingerprints,
                    conversation_fingerprints=conversation_fingerprints,
                    file_keys=file_keys,
                )
            )
        return events

    def _filter_events(
        self,
        events: list[WorkEvent],
        *,
        source_events: list[CollectedSourceEvent],
    ) -> tuple[list[WorkEvent], list[str], list[dict[str, Any]]]:
        kept_events, raw_diagnostics = filter_work_events_with_diagnostics(
            events,
            self.config,
        )
        diagnostics: list[dict[str, Any]] = []
        warnings: list[str] = []
        for raw in raw_diagnostics:
            event = events[raw.item_index]
            event_source_ids = set(event.source_event_ids or [event.event_id])
            matching_sources = [
                item
                for item in source_events
                if event_source_ids.intersection(
                    item.event.source_event_ids or [item.event.event_id]
                )
            ]
            diagnostic = {
                "stage": "merged_filter",
                "kind": raw.kind,
                "source_files": list(
                    dict.fromkeys(item.source_file for item in matching_sources)
                ),
                "source_people": list(
                    dict.fromkeys(item.person_name for item in matching_sources)
                ),
                "event_id": event.event_id,
                "event_title": event.title,
            }
            diagnostics.append(diagnostic)
            warnings.append(_render_filter_warning(diagnostic))
        return kept_events, warnings, diagnostics

    def _start_collected_merge_trace(self, target_date: str, input_dir: Path) -> None:
        self._collected_merge_trace_steps = []
        self._collected_merge_trace_call_index = 0
        self._collected_merge_source_audit = []
        self._collected_merge_filter_diagnostics = []
        self._collected_merge_failure_warnings = []
        if not self.config.collected_merge_trace_enabled:
            self._collected_merge_trace_dir = None
            return
        root = self.config.collected_merge_trace_root
        trace_root = root if root.is_absolute() else self.cwd / root
        date_dir = trace_root / target_date
        if input_dir.name != target_date.split("-")[-1]:
            date_dir = date_dir / input_dir.name
        date_dir.mkdir(parents=True, exist_ok=True)
        for pattern in (
            "step-*.json",
            "step-*-prompt.txt",
            "summary.json",
            "summary.md",
            "source-audit.json",
        ):
            for stale_path in date_dir.glob(pattern):
                if stale_path.is_file():
                    stale_path.unlink()
        self._collected_merge_trace_dir = date_dir

    def _start_collected_merge_trace_attempt(
        self,
        *,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        deterministic_groups: list[list[str]],
        rolling_step_index: int,
        attempt_index: int,
        retry_reason: str,
        stage: str = "content_merge",
        prompt_override: str | None = None,
        extra_trace: dict[str, Any] | None = None,
    ) -> int:
        if self._collected_merge_trace_dir is None:
            return 0
        self._collected_merge_trace_call_index += 1
        prompt = prompt_override or (
            build_collected_grouping_prompt(
                target_date,
                source_events,
                deterministic_groups,
                config=self.config,
            )
            if stage.startswith("candidate_")
            else build_collected_render_prompt(
                target_date,
                source_events,
                deterministic_groups,
                config=self.config,
            )
        )
        step = {
            "step_index": self._collected_merge_trace_call_index,
            "status": "started",
            "stage": stage,
            "rolling_step_index": rolling_step_index,
            "attempt_index": attempt_index,
            "retry_reason": retry_reason,
            "prompt_chars": len(prompt),
            "prompt_estimated_tokens": _estimate_prepared_model_prompt_tokens(prompt),
            "input_limit_tokens": self.config.max_model_input_tokens,
            "prompt_file": f"step-{self._collected_merge_trace_call_index:03d}-prompt.txt",
            "input": collected_merge_source_metrics(source_events),
            "input_events": [item.to_dict() for item in source_events],
            "deterministic_groups": [list(group) for group in deterministic_groups],
            "content_fit": [
                {
                    "draft_id": item.draft_id,
                    "source_file": item.source_file,
                    "original_chars": (
                        item.prompt_original_content_chars
                        if item.prompt_original_content_chars is not None
                        else len(clean_text(item.event.content))
                    ),
                    "sent_chars": len(clean_text(item.event.content)),
                    "shortened": bool(
                        item.prompt_original_content_chars is not None
                        and len(clean_text(item.event.content))
                        < item.prompt_original_content_chars
                    ),
                }
                for item in source_events
            ],
            "candidate_summary_sources": [
                {
                    "draft_id": item.draft_id,
                    "source": item.candidate_summary_source,
                    "source_event_ids": list(item.event.source_event_ids),
                }
                for item in source_events
                if item.candidate_summary_source
            ],
        }
        if extra_trace:
            step.update(extra_trace)
        self._collected_merge_trace_steps.append(step)
        self._write_collected_merge_trace_step(step)
        (self._collected_merge_trace_dir / step["prompt_file"]).write_text(
            prompt,
            encoding="utf-8",
        )
        return self._collected_merge_trace_call_index

    def _record_collected_grouping_trace_success(
        self,
        *,
        step_index: int,
        grouping_result: CollectedGroupingResult,
        source_coverage_error: str = "",
    ) -> None:
        step = self._collected_merge_trace_step(step_index)
        if step is None:
            return
        step.update(
            {
                "status": "success",
                "raw_group_count": len(grouping_result.groups),
                "raw_result": grouping_result.to_dict(),
                "source_coverage_error": source_coverage_error,
            }
        )
        self._write_collected_merge_trace_step(step)

    def _record_collected_merge_trace_success(
        self,
        *,
        step_index: int,
        merge_result: CollectedMergeResult,
        missing_summary: dict[str, int],
        coverage_error: str = "",
    ) -> None:
        step = self._collected_merge_trace_step(step_index)
        if step is None:
            return
        step.update(
            {
                "status": "success",
                "raw_group_count": len(merge_result.groups),
                "raw_group_metrics": collected_merge_group_metrics(merge_result),
                "missing_required_field_summary": missing_summary,
                "source_coverage_error": coverage_error,
                "raw_result": merge_result.to_dict(),
            }
        )
        self._write_collected_merge_trace_step(step)

    def _record_collected_merge_trace_failure(
        self,
        *,
        step_index: int,
        error: Exception,
        retryable: bool,
    ) -> None:
        step = self._collected_merge_trace_step(step_index)
        if step is None:
            return
        step.update(
            {
                "status": "failed",
                "error": {
                    "type": type(error).__name__,
                    "summary": str(error),
                    "retryable": retryable,
                },
            }
        )
        self._write_collected_merge_trace_step(step)

    def _collected_merge_trace_step(
        self,
        step_index: int,
    ) -> dict[str, Any] | None:
        return next(
            (
                step
                for step in self._collected_merge_trace_steps
                if step.get("step_index") == step_index
            ),
            None,
        )

    def _record_collected_merge_trace_final(
        self,
        *,
        repaired_result: CollectedMergeResult,
        merged_events_before_filters: list[WorkEvent],
        merged_events_after_sensitive: list[WorkEvent],
        retained_events: list[WorkEvent],
        repair_warnings: list[str],
        boundary_warnings: list[str],
        metadata_warnings: list[str],
        sensitive_warnings: list[str],
        retention_warnings: list[str],
    ) -> None:
        if self._collected_merge_trace_dir is None or not self._collected_merge_trace_steps:
            return
        step = self._collected_merge_trace_steps[-1]
        step.update(
            {
                "repaired_group_count": len(repaired_result.groups),
                "repaired_group_metrics": collected_merge_group_metrics(repaired_result),
                "materialized_metrics": collected_merge_work_event_metrics(
                    merged_events_before_filters,
                ),
                "after_sensitive_metrics": collected_merge_work_event_metrics(
                    merged_events_after_sensitive,
                ),
                "retained_metrics": collected_merge_work_event_metrics(retained_events),
                "repair_warnings": repair_warnings,
                "boundary_warnings": boundary_warnings,
                "metadata_warnings": metadata_warnings,
                "sensitive_warnings": sensitive_warnings,
                "retention_warnings": retention_warnings,
                "dropped_by_retention": [
                    {
                        "title": event.title,
                        "source_event_ids": list(event.source_event_ids),
                        "retention_reason": event.retention_reason,
                        "retention_detail": event.retention_detail,
                        "rejection_reason": retention_rejection_reason_for_event(event),
                    }
                    for event in merged_events_after_sensitive
                    if retention_rejection_reason_for_event(event)
                ],
                "repaired_result": repaired_result.to_dict(),
                "retained_events": [event.to_dict() for event in retained_events],
            }
        )
        self._write_collected_merge_trace_step(step)

    def _write_collected_merge_trace_step(self, step: dict[str, Any]) -> None:
        if self._collected_merge_trace_dir is None:
            return
        path = self._collected_merge_trace_dir / f"step-{step['step_index']:03d}.json"
        path.write_text(dump_json(step, pretty=True), encoding="utf-8")

    def _write_collected_merge_source_audit(
        self,
        *,
        target_date: str,
        input_dir: Path,
        source_file_count: int,
        skipped_file_count: int,
        partial_file_count: int,
        parsed_event_count: int,
        model_input_event_count: int,
    ) -> None:
        if self._collected_merge_trace_dir is None:
            return
        payload = {
            "target_date": target_date,
            "input_dir": str(input_dir.resolve()),
            "source_file_count": source_file_count,
            "skipped_file_count": skipped_file_count,
            "partial_file_count": partial_file_count,
            "parsed_event_count": parsed_event_count,
            "model_input_event_count": model_input_event_count,
            "source_files": self._collected_merge_source_audit,
            "filter_diagnostics": self._collected_merge_filter_diagnostics,
        }
        (self._collected_merge_trace_dir / "source-audit.json").write_text(
            dump_json(payload, pretty=True),
            encoding="utf-8",
        )

    def _write_collected_merge_trace_summary(
        self,
        *,
        status: str,
        target_date: str,
        input_dir: Path,
        output_path: Path | None,
        source_file_count: int,
        source_event_count: int,
        merged_event_count: int,
        skipped_file_count: int,
        partial_file_count: int,
        warning_messages: list[str],
        quality_summary: CollectedMergeQualitySummary,
    ) -> None:
        if self._collected_merge_trace_dir is None:
            return
        summary = {
            "status": status,
            "target_date": target_date,
            "input_dir": str(input_dir.resolve()),
            "output_path": None if output_path is None else str(output_path.resolve()),
            "source_file_count": source_file_count,
            "source_event_count": source_event_count,
            "merged_event_count": merged_event_count,
            "skipped_file_count": skipped_file_count,
            "partial_file_count": partial_file_count,
            "quality_summary": quality_summary.to_dict(),
            "source_files": self._collected_merge_source_audit,
            "filter_diagnostics": self._collected_merge_filter_diagnostics,
            "failed_step_indexes": [
                step["step_index"]
                for step in self._collected_merge_trace_steps
                if step.get("status") == "failed"
            ],
            "warning_messages": warning_messages,
            "steps": self._collected_merge_trace_steps,
        }
        (self._collected_merge_trace_dir / "summary.json").write_text(
            dump_json(summary, pretty=True),
            encoding="utf-8",
        )
        (self._collected_merge_trace_dir / "summary.md").write_text(
            render_collected_merge_trace_summary(summary),
            encoding="utf-8",
        )

    def _resolve_self_identity(self) -> SelfIdentity:
        result = self.command_runner(("lark-cli", "auth", "status"), cwd=self.cwd)
        if getattr(result, "returncode", 1) != 0:
            stderr = (getattr(result, "stderr", "") or "").strip()
            raise StoreWriteError(stderr or "Failed to resolve current Feishu user identity.")
        try:
            payload = json.loads(getattr(result, "stdout", "") or "")
        except json.JSONDecodeError as exc:
            raise StoreWriteError("lark-cli auth status did not return valid JSON.") from exc
        user = payload.get("identities", {}).get("user", {})
        open_id = str(user.get("openId") or "").strip()
        display_name = str(user.get("userName") or open_id).strip()
        if payload.get("identity") != "user" or not open_id:
            raise StoreWriteError("Failed to resolve current Feishu user identity.")
        return SelfIdentity(open_id=open_id, display_name=display_name, source="lark-cli")

    def _run_command(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(args),
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
        )


def extract_person_name_from_filename(filename: str, *, target_date: str = "") -> str:
    parsed = parse_worktrace_markdown_filename(filename)
    if parsed.is_merged:
        return ""
    return extract_source_name_from_filename(filename, target_date=target_date)


def extract_source_name_from_filename(filename: str, *, target_date: str = "") -> str:
    parsed = parse_worktrace_markdown_filename(filename)
    if parsed.suffix != ".md":
        return ""
    if not parsed.target_date or not parsed.owner_name:
        return ""
    if target_date and parsed.target_date != target_date:
        return ""
    return parsed.owner_name.strip()


def _classify_source_markdown(
    filename: str,
    events: list[WorkEvent],
) -> str:
    parsed = parse_worktrace_markdown_filename(filename)
    if parsed.is_merged:
        return "upstream_merged"
    if any(
        event.workstream_name
        or event.action_labels
        or event.self_relations
        or event.evidence_fingerprints
        or event.file_keys
        for event in events
    ):
        return "enhanced_personal"
    return "legacy_personal"


def _render_filter_warning(diagnostic: dict[str, Any]) -> str:
    diagnostic_stage = str(diagnostic.get("stage", ""))
    stage = "source" if diagnostic_stage.startswith("source_") else "merged"
    source_file = str(diagnostic.get("source_file", ""))
    if not source_file:
        source_file = ",".join(
            str(item) for item in diagnostic.get("source_files", []) if item
        )
    location = source_file or "unknown-source"
    event_id = str(diagnostic.get("event_id", ""))
    title = str(diagnostic.get("event_title", "")) or "(empty title)"
    return (
        f"Filtered {diagnostic.get('kind', 'unknown')} {stage} event: "
        f"{location}#{event_id} ({title})."
    )


def collected_merge_missing_field_summary(
    merge_result: CollectedMergeResult,
) -> dict[str, int]:
    summary = {
        "title": 0,
        "content": 0,
        "object_hint": 0,
        "retention_reason": 0,
        "retention_detail": 0,
    }
    for group in merge_result.groups:
        if not clean_text(group.title):
            summary["title"] += 1
        if not clean_text(group.content):
            summary["content"] += 1
        if not clean_text(group.object_hint):
            summary["object_hint"] += 1
        if clean_text(group.retention_reason) not in RETENTION_REASONS:
            summary["retention_reason"] += 1
        if not clean_text(group.retention_detail):
            summary["retention_detail"] += 1
    return summary


def collected_merge_coverage_error(merge_result: CollectedMergeResult) -> str:
    errors: list[str] = []
    for group in merge_result.groups:
        if group.covered_draft_ids is None:
            continue
        expected = set(group.draft_ids)
        covered = set(group.covered_draft_ids)
        if covered != expected or len(group.covered_draft_ids) != len(covered):
            errors.append(
                f"group={group.group_id} covered_draft_ids_mismatch"
            )
            continue
        if not group.fact_items:
            errors.append(f"group={group.group_id} fact_items_empty")
            continue
        fact_sources: set[str] = set()
        invalid_fact = False
        for fact in group.fact_items:
            sources = set(fact.source_draft_ids)
            if not clean_text(fact.text) or not sources or not sources.issubset(expected):
                invalid_fact = True
                break
            fact_sources.update(sources)
        if invalid_fact:
            errors.append(f"group={group.group_id} fact_item_invalid")
        elif fact_sources != expected:
            errors.append(f"group={group.group_id} fact_source_coverage_mismatch")
    return "; ".join(errors)


def collected_merge_partition_error(
    merge_result: CollectedMergeResult,
    expected_draft_ids: list[str],
    *,
    locked_groups: list[list[str]],
) -> str:
    expected = set(expected_draft_ids)
    flattened = [
        draft_id
        for group in merge_result.groups
        for draft_id in group.draft_ids
    ]
    counts = Counter(flattened)
    details: list[str] = []
    missing = sorted(expected.difference(counts))
    unknown = sorted(set(counts).difference(expected))
    duplicates = sorted(
        draft_id for draft_id, count in counts.items() if count > 1
    )
    if missing:
        details.append(f"missing_draft_ids={missing}")
    if unknown:
        details.append(f"unknown_draft_ids={unknown}")
    if duplicates:
        details.append(f"duplicate_draft_ids={duplicates}")

    locked_ids = {
        draft_id for group in locked_groups for draft_id in group
    }
    if locked_groups and locked_ids == expected:
        expected_groups = Counter(
            frozenset(group) for group in locked_groups if group
        )
        actual_groups = Counter(
            frozenset(group.draft_ids) for group in merge_result.groups
            if group.draft_ids
        )
        if actual_groups != expected_groups:
            details.append("locked_groups_changed")
    return "; ".join(details)


def format_collected_merge_missing_field_summary(summary: dict[str, int]) -> str:
    return ", ".join(
        f"{field}={count}"
        for field, count in summary.items()
        if count
    ) or "none"


def derive_collected_merge_retention_detail(
    source_events: list[CollectedSourceEvent],
) -> str:
    details = [
        clean_text(item.event.retention_detail)
        for item in source_events
        if clean_text(item.event.retention_detail)
    ]
    if not details:
        details = [
            clean_text(item.event.content)
            for item in source_events
            if clean_text(item.event.content)
        ]
    if not details:
        return ""
    unique_details = list(dict.fromkeys(details))
    if len(unique_details) == 1:
        return unique_details[0]
    selected = sorted(unique_details, key=lambda value: (-len(value), value))[:3]
    return "；".join(selected)


def collected_merge_source_metrics(
    source_events: list[CollectedSourceEvent],
) -> dict[str, Any]:
    return {
        "event_count": len(source_events),
        "synthetic_event_count": sum(
            1
            for item in source_events
            if item.source_file.startswith("__rolling_collected_merge_step_")
        ),
        "source_file_count": len({item.source_file for item in source_events}),
        "source_id_count": len(collected_merge_source_ids_from_source_events(source_events)),
        "text_metrics": collected_merge_text_metrics(
            [
                {
                    "title": item.event.title,
                    "content": item.event.content,
                    "object_hint": item.event.object_hint,
                    "retention_detail": item.event.retention_detail,
                }
                for item in source_events
            ]
        ),
    }


def collected_merge_group_metrics(
    merge_result: CollectedMergeResult,
) -> dict[str, Any]:
    return {
        "group_count": len(merge_result.groups),
        "draft_ref_count": sum(len(group.draft_ids) for group in merge_result.groups),
        "missing_required_fields": collected_merge_missing_field_summary(merge_result),
        "text_metrics": collected_merge_text_metrics(
            [
                {
                    "title": group.title,
                    "content": group.content,
                    "object_hint": group.object_hint,
                    "retention_detail": group.retention_detail,
                }
                for group in merge_result.groups
            ]
        ),
    }


def collected_merge_work_event_metrics(events: list[WorkEvent]) -> dict[str, Any]:
    rejection_reasons = Counter(
        reason
        for event in events
        for reason in [retention_rejection_reason_for_event(event)]
        if reason
    )
    return {
        "event_count": len(events),
        "source_id_count": len(collected_merge_source_ids_from_work_events(events)),
        "retention_rejection_reasons_if_filtered_now": dict(rejection_reasons),
        "text_metrics": collected_merge_text_metrics(
            [
                {
                    "title": event.title,
                    "content": event.content,
                    "object_hint": event.object_hint,
                    "retention_detail": event.retention_detail,
                }
                for event in events
            ]
        ),
    }


def collected_merge_source_ids_from_source_events(
    source_events: list[CollectedSourceEvent],
) -> set[str]:
    values: set[str] = set()
    for source_event in source_events:
        values.update(source_event.event.source_event_ids or [source_event.event.event_id])
    return {value for value in values if clean_text(value)}


def collected_merge_source_ids_from_work_events(events: list[WorkEvent]) -> set[str]:
    values: set[str] = set()
    for event in events:
        values.update(event.source_event_ids or [event.event_id])
    return {value for value in values if clean_text(value)}


def build_collected_quality_summary(
    parsed_source_events: list[CollectedSourceEvent],
    filtered_source_events: list[CollectedSourceEvent],
    output_events: list[WorkEvent],
    *,
    counters: Counter[str] | None = None,
) -> CollectedMergeQualitySummary:
    counters = counters or Counter()
    source_id_sets = [
        {
            value
            for value in (
                item.event.source_event_ids or [item.event.event_id]
            )
            if clean_text(value)
        }
        for item in filtered_source_events
    ]
    output_id_sets = [
        {
            value
            for value in (event.source_event_ids or [event.event_id])
            if clean_text(value)
        }
        for event in output_events
    ]
    source_counts_per_output = [
        sum(
            1
            for source_ids in source_id_sets
            if source_ids and source_ids.issubset(output_ids)
        )
        for output_ids in output_id_sets
    ]
    covered_source_count = sum(
        1
        for source_ids in source_id_sets
        if source_ids
        and any(source_ids.issubset(output_ids) for output_ids in output_id_sets)
    )
    input_content_chars = sum(
        len(clean_text(item.event.content)) for item in parsed_source_events
    )
    output_content_chars = sum(
        len(clean_text(event.content)) for event in output_events
    )
    source_report_owners = {
        owner.strip()
        for item in filtered_source_events
        for owner in [
            *item.event.source_report_owners,
            item.source_report_owner,
        ]
        if owner.strip()
    }
    input_event_count = len(parsed_source_events)
    filtered_event_count = len(filtered_source_events)
    return CollectedMergeQualitySummary(
        input_event_count=input_event_count,
        filtered_event_count=filtered_event_count,
        output_event_count=len(output_events),
        multi_source_group_count=sum(
            count > 1 for count in source_counts_per_output
        ),
        singleton_group_count=sum(
            count == 1 for count in source_counts_per_output
        ),
        max_source_events_per_group=max(source_counts_per_output, default=0),
        input_content_chars=input_content_chars,
        output_content_chars=output_content_chars,
        event_count_output_input_ratio=_quality_ratio(
            len(output_events),
            input_event_count,
        ),
        content_chars_output_input_ratio=_quality_ratio(
            output_content_chars,
            input_content_chars,
        ),
        source_event_coverage_ratio=_quality_ratio(
            covered_source_count,
            filtered_event_count,
        ),
        source_report_owner_count=len(source_report_owners),
        high_risk_group_count=int(counters["high_risk_group_count"]),
        reviewed_group_count=int(counters["reviewed_group_count"]),
        review_split_group_count=int(counters["review_split_group_count"]),
        content_retry_count=int(counters["content_retry_count"]),
        shortened_prompt_count=int(counters["shortened_prompt_count"]),
        review_required=bool(counters["review_required"]),
    )


def aggregate_collected_quality_summaries(
    summaries: list[CollectedMergeQualitySummary],
) -> CollectedMergeQualitySummary:
    if not summaries:
        return CollectedMergeQualitySummary()
    input_event_count = sum(item.input_event_count for item in summaries)
    filtered_event_count = sum(item.filtered_event_count for item in summaries)
    output_event_count = sum(item.output_event_count for item in summaries)
    input_content_chars = sum(item.input_content_chars for item in summaries)
    output_content_chars = sum(item.output_content_chars for item in summaries)
    covered_source_count = sum(
        item.source_event_coverage_ratio * item.filtered_event_count
        for item in summaries
    )
    return CollectedMergeQualitySummary(
        input_event_count=input_event_count,
        filtered_event_count=filtered_event_count,
        output_event_count=output_event_count,
        multi_source_group_count=sum(
            item.multi_source_group_count for item in summaries
        ),
        singleton_group_count=sum(item.singleton_group_count for item in summaries),
        max_source_events_per_group=max(
            (item.max_source_events_per_group for item in summaries),
            default=0,
        ),
        input_content_chars=input_content_chars,
        output_content_chars=output_content_chars,
        event_count_output_input_ratio=_quality_ratio(
            output_event_count,
            input_event_count,
        ),
        content_chars_output_input_ratio=_quality_ratio(
            output_content_chars,
            input_content_chars,
        ),
        source_event_coverage_ratio=_quality_ratio(
            covered_source_count,
            filtered_event_count,
        ),
        source_report_owner_count=sum(
            item.source_report_owner_count for item in summaries
        ),
        high_risk_group_count=sum(item.high_risk_group_count for item in summaries),
        reviewed_group_count=sum(item.reviewed_group_count for item in summaries),
        review_split_group_count=sum(
            item.review_split_group_count for item in summaries
        ),
        content_retry_count=sum(item.content_retry_count for item in summaries),
        shortened_prompt_count=sum(item.shortened_prompt_count for item in summaries),
        review_required=any(item.review_required for item in summaries),
    )


def _quality_ratio(numerator: int | float, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(float(numerator) / denominator, 4)


def collected_merge_text_metrics(rows: list[dict[str, str]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in ("title", "content", "object_hint", "retention_detail"):
        lengths = [len(clean_text(row.get(field, ""))) for row in rows]
        result[f"{field}_avg_len"] = _average(lengths)
        result[f"{field}_median_len"] = _median(lengths)
        result[f"{field}_min_len"] = min(lengths) if lengths else 0
        result[f"{field}_max_len"] = max(lengths) if lengths else 0
    return result


def render_collected_merge_trace_summary(summary: dict[str, Any]) -> str:
    quality = summary.get("quality_summary", {})
    lines = [
        f"# Collected Merge Trace · {summary['target_date']}",
        "",
        "## Summary",
        "",
        f"- Status: {summary.get('status', '')}",
        f"- Source files: {summary['source_file_count']}",
        f"- Skipped files: {summary.get('skipped_file_count', 0)}",
        f"- Partial files: {summary.get('partial_file_count', 0)}",
        f"- Source events: {summary['source_event_count']}",
        f"- Merged events: {summary['merged_event_count']}",
        f"- Output: `{summary['output_path']}`",
        "",
        "## Quality Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Input events | {quality.get('input_event_count', 0)} |",
        f"| Events after filters | {quality.get('filtered_event_count', 0)} |",
        f"| Output events | {quality.get('output_event_count', 0)} |",
        f"| Multi-source groups | {quality.get('multi_source_group_count', 0)} |",
        f"| Singleton groups | {quality.get('singleton_group_count', 0)} |",
        f"| Max source events per group | {quality.get('max_source_events_per_group', 0)} |",
        f"| Input content chars | {quality.get('input_content_chars', 0)} |",
        f"| Output content chars | {quality.get('output_content_chars', 0)} |",
        f"| Event output/input ratio | {quality.get('event_count_output_input_ratio', 0)} |",
        f"| Content output/input ratio | {quality.get('content_chars_output_input_ratio', 0)} |",
        f"| Source event coverage ratio | {quality.get('source_event_coverage_ratio', 0)} |",
        f"| Source report owners | {quality.get('source_report_owner_count', 0)} |",
        f"| High-risk groups | {quality.get('high_risk_group_count', 0)} |",
        f"| Reviewed groups | {quality.get('reviewed_group_count', 0)} |",
        f"| Review-split groups | {quality.get('review_split_group_count', 0)} |",
        f"| Content retries | {quality.get('content_retry_count', 0)} |",
        f"| Shortened prompts | {quality.get('shortened_prompt_count', 0)} |",
        f"| Review required | {str(bool(quality.get('review_required', False))).lower()} |",
        "",
        "## Step Metrics",
        "",
        "| Step | Stage | Status | Batch/Rolling step | Attempt | Retry reason | Estimated tokens | Input limit | Prompt chars | Input events | Raw groups | Retained events | Error |",
        "|---:|---|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for step in summary["steps"]:
        retained_events = step.get("retained_metrics", {}).get("event_count", "")
        error_summary = str(step.get("error", {}).get("summary", "")).replace("|", "\\|")
        lines.append(
            "| {step} | {stage} | {status} | {rolling} | {attempt} | {retry_reason} | "
            "{prompt_tokens} | {input_limit} | {prompt_chars} | {input_events} | "
            "{raw_groups} | {retained_events} | {error} |".format(
                step=step.get("step_index", ""),
                stage=step.get("stage", ""),
                status=step.get("status", ""),
                rolling=step.get("rolling_step_index", ""),
                attempt=step.get("attempt_index", ""),
                retry_reason=step.get("retry_reason", ""),
                prompt_tokens=step.get("prompt_estimated_tokens", ""),
                input_limit=step.get("input_limit_tokens", ""),
                prompt_chars=step.get("prompt_chars", ""),
                input_events=step.get("input", {}).get("event_count", ""),
                raw_groups=step.get("raw_group_count", ""),
                retained_events=retained_events,
                error=error_summary,
            )
        )
    lines.extend(["", "## Source Files", ""])
    lines.extend(
        [
            "| File | Format | Status | Declared | Parsed | Model input | Sensitive | Excluded | Retention |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for item in summary.get("source_files", []):
        lines.append(
            "| {file} | {format} | {status} | {declared} | {parsed} | {model_input} | "
            "{sensitive} | {excluded} | {retention} |".format(
                file=item.get("source_file", ""),
                format=item.get("format", ""),
                status=item.get("status", ""),
                declared=item.get("declared_event_count", ""),
                parsed=item.get("parsed_event_count", ""),
                model_input=item.get("model_input_event_count", ""),
                sensitive=item.get("sensitive_filtered_count", 0),
                excluded=item.get("excluded_filtered_count", 0),
                retention=item.get("retention_filtered_count", 0),
            )
        )
    lines.extend(["", "## Filter Diagnostics", ""])
    for item in summary.get("filter_diagnostics", []):
        lines.append(f"- {_render_filter_warning(item)}")
    lines.extend(["", "## Warnings", ""])
    for warning in summary["warning_messages"]:
        lines.append(f"- {warning}")
    return "\n".join(lines) + "\n"


def _average(values: list[int]) -> float:
    if not values:
        return 0
    return round(sum(values) / len(values), 2)


def _median(values: list[int]) -> float:
    if not values:
        return 0
    sorted_values = sorted(values)
    middle = len(sorted_values) // 2
    if len(sorted_values) % 2:
        return float(sorted_values[middle])
    return round((sorted_values[middle - 1] + sorted_values[middle]) / 2, 2)


def should_skip_input_file(path: Path, *, output_filename: str = "") -> bool:
    if (
        path.name == "_merged.md"
        or path.name.endswith("-merge-omitted-events.md")
        or path.name.startswith(".")
        or path.is_dir()
        or (output_filename and path.name == output_filename)
    ):
        return True
    return False


def summarize_self_delivery_status(outputs: list[CollectedMergeOutput]) -> str:
    statuses = {
        output.self_delivery_status for output in outputs if output.self_delivery_status
    }
    if "failed" in statuses:
        return "failed"
    if "success" in statuses:
        return "success"
    return ""


def build_collected_draft_id(filename: str, index: int, event_id: str) -> str:
    safe_event_id = event_id.strip() or f"event-{index}"
    return f"{filename}#{index}:{safe_event_id}"


def build_synthetic_collected_source_events(
    target_date: str,
    events: list[WorkEvent],
    *,
    step_index: int,
    merge_owner_person: str = "",
) -> list[CollectedSourceEvent]:
    source_file = f"__rolling_collected_merge_step_{step_index}.md"
    owner_name = merge_owner_person.strip()
    return [
        CollectedSourceEvent(
            draft_id=build_collected_draft_id(source_file, index, event.event_id),
            person_name="rolling-collected-merge",
            source_file=source_file,
            event=replace(event, date=target_date),
            is_merge_owner_source=bool(
                owner_name
                and owner_name in {name.strip() for name in event.source_people}
            ),
        )
        for index, event in enumerate(events, start=1)
    ]


def build_collected_relation_components(
    source_events: list[CollectedSourceEvent],
    deterministic_groups: list[list[str]],
) -> list[list[CollectedSourceEvent]]:
    event_by_id = {item.draft_id: item for item in source_events}
    order = {item.draft_id: index for index, item in enumerate(source_events)}
    parent = {item.draft_id: item.draft_id for item in source_events}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        if order[left_root] <= order[right_root]:
            parent[right_root] = left_root
        else:
            parent[left_root] = right_root

    for group in deterministic_groups:
        known = [draft_id for draft_id in group if draft_id in event_by_id]
        for draft_id in known[1:]:
            union(known[0], draft_id)

    for field_name in (
        "evidence_fingerprints",
        "file_keys",
        "conversation_fingerprints",
    ):
        members_by_value: dict[str, list[str]] = defaultdict(list)
        for item in source_events:
            for value in getattr(item.event, field_name):
                if value:
                    members_by_value[value].append(item.draft_id)
        for members in members_by_value.values():
            for draft_id in members[1:]:
                union(members[0], draft_id)

    grouped: dict[str, list[CollectedSourceEvent]] = defaultdict(list)
    for item in source_events:
        grouped[find(item.draft_id)].append(item)
    return sorted(
        grouped.values(),
        key=lambda items: min(order[item.draft_id] for item in items),
    )


def build_grouping_summary_events(
    target_date: str,
    source_events: list[CollectedSourceEvent],
    groups: list[CollectedGroupingGroup],
    *,
    depth: int,
) -> tuple[list[CollectedSourceEvent], dict[str, list[str]]]:
    source_by_id = {item.draft_id: item for item in source_events}
    summaries: list[CollectedSourceEvent] = []
    source_ids_by_synthetic: dict[str, list[str]] = {}
    for index, group in enumerate(groups, start=1):
        items = [
            source_by_id[draft_id]
            for draft_id in group.draft_ids
            if draft_id in source_by_id
        ]
        if not items:
            continue
        workstream_names = _dedupe([item.event.workstream_name for item in items])
        normalized_workstreams = {
            "".join(name.casefold().split()) for name in workstream_names
        }
        is_singleton = len(items) == 1
        has_model_summary = all(
            clean_text(value)
            for value in (
                group.summary_title,
                group.summary_content,
                group.summary_object_hint,
            )
        )
        if is_singleton:
            summary_title = items[0].event.title
            summary_content = items[0].event.content
            summary_object_hint = items[0].event.object_hint
            summary_source = "original_singleton"
        elif has_model_summary:
            summary_title = clean_text(group.summary_title)
            summary_content = clean_text(group.summary_content)
            summary_object_hint = clean_text(group.summary_object_hint)
            summary_source = "model"
        else:
            summary_title = choose_preferred_text(
                [item.event.title for item in items]
            )
            summary_content = build_balanced_group_content(items)
            summary_object_hint = choose_preferred_text(
                [item.event.object_hint for item in items]
            )
            summary_source = "balanced_fallback"
        synthetic_id = f"__grouping_summary_{depth}_{index}"
        source_ids_by_synthetic[synthetic_id] = list(group.draft_ids)
        summaries.append(
            CollectedSourceEvent(
                draft_id=synthetic_id,
                person_name="grouping-summary",
                source_file=f"__grouping_summary_{depth}.md",
                candidate_summary_source=summary_source,
                is_merge_owner_source=any(
                    item.is_merge_owner_source for item in items
                ),
                event=WorkEvent(
                    date=target_date,
                    event_id=stable_event_id(
                        target_date,
                        list(group.draft_ids),
                        "grouping-summary",
                    ),
                    title=summary_title,
                    content=summary_content,
                    source_people=_dedupe(
                        [
                            person
                            for item in items
                            for person in (
                                item.event.source_people or [item.person_name]
                            )
                        ]
                    ),
                    source_event_ids=_dedupe(
                        [
                            source_id
                            for item in items
                            for source_id in (
                                item.event.source_event_ids
                                or [item.event.event_id]
                            )
                        ]
                    ),
                    source_report_owners=_dedupe(
                        [
                            owner
                            for item in items
                            for owner in [
                                *item.event.source_report_owners,
                                item.source_report_owner,
                            ]
                            if owner
                        ]
                    ),
                    object_hint=summary_object_hint,
                    retention_reason=choose_preferred_text(
                        [item.event.retention_reason for item in items]
                    ),
                    retention_detail=derive_collected_merge_retention_detail(items),
                    workstream_name=(
                        workstream_names[0]
                        if len(normalized_workstreams) <= 1 and workstream_names
                        else ""
                    ),
                    action_labels=_dedupe(
                        [
                            label
                            for item in items
                            for label in item.event.action_labels
                        ]
                    ),
                    self_relations=_dedupe(
                        [
                            relation
                            for item in items
                            for relation in item.event.self_relations
                        ]
                    ),
                    evidence_fingerprints=_dedupe(
                        [
                            value
                            for item in items
                            for value in item.event.evidence_fingerprints
                        ]
                    ),
                    conversation_fingerprints=_dedupe(
                        [
                            value
                            for item in items
                            for value in item.event.conversation_fingerprints
                        ]
                    ),
                    file_keys=_dedupe(
                        [
                            value
                            for item in items
                            for value in item.event.file_keys
                        ]
                    ),
                ),
            )
        )
    return summaries, source_ids_by_synthetic


_SENTENCE_PART_RE = re.compile(r".+?(?:[。！？!?；;]+|\n+|$)", re.DOTALL)
_CONTENT_SPLIT_BOUNDARY_RE = re.compile(r"[。！？!?；;\n]")


def _preferred_content_split_index(value: str, max_length: int) -> int:
    if len(value) <= max_length:
        return len(value)
    prefix = value[:max_length]
    boundaries = [match.end() for match in _CONTENT_SPLIT_BOUNDARY_RE.finditer(prefix)]
    if boundaries and boundaries[-1] >= max_length // 2:
        return boundaries[-1]
    return max_length


def _apply_balanced_source_content_quota(
    source_events: list[CollectedSourceEvent],
    *,
    per_source_char_quota: int,
) -> list[CollectedSourceEvent]:
    indexes_by_source: dict[str, list[int]] = {}
    for index, item in enumerate(source_events):
        source_key = item.source_file or item.person_name or item.draft_id
        indexes_by_source.setdefault(source_key, []).append(index)

    allocations = [0] * len(source_events)
    for indexes in indexes_by_source.values():
        lengths = [
            len(clean_text(source_events[index].event.content))
            for index in indexes
        ]
        source_allocations = _allocate_balanced_lengths(
            lengths,
            per_source_char_quota,
        )
        for index, allocated in zip(indexes, source_allocations, strict=True):
            allocations[index] = allocated

    fitted: list[CollectedSourceEvent] = []
    for item, allocated in zip(source_events, allocations, strict=True):
        original_content = clean_text(item.event.content)
        fitted.append(
            replace(
                item,
                event=replace(
                    item.event,
                    content=_balanced_text_excerpt(original_content, allocated),
                ),
                prompt_original_content_chars=len(original_content),
            )
        )
    return fitted


def _allocate_balanced_lengths(lengths: list[int], budget: int) -> list[int]:
    allocations = [0] * len(lengths)
    remaining = list(range(len(lengths)))
    remaining_budget = max(0, budget)
    while remaining and remaining_budget > 0:
        share, extra = divmod(remaining_budget, len(remaining))
        completed = [index for index in remaining if lengths[index] <= share]
        if completed:
            for index in completed:
                allocations[index] = lengths[index]
                remaining_budget -= lengths[index]
                remaining.remove(index)
            continue
        for position, index in enumerate(remaining):
            allocations[index] = min(
                lengths[index],
                share + (1 if position < extra else 0),
            )
        break
    return allocations


def _balanced_text_excerpt(value: str, limit: int) -> str:
    cleaned = clean_text(value)
    if limit <= 0:
        return ""
    if len(cleaned) <= limit:
        return cleaned
    if limit <= 3:
        return cleaned[:limit]

    parts = [
        clean_text(match.group(0))
        for match in _SENTENCE_PART_RE.finditer(cleaned)
        if clean_text(match.group(0))
    ]
    available = limit - 3
    prefix_budget = available // 2
    prefix_parts: list[str] = []
    prefix_chars = 0
    prefix_count = 0
    for part in parts:
        added_chars = len(part) + (1 if prefix_parts else 0)
        if prefix_chars + added_chars > prefix_budget:
            break
        prefix_parts.append(part)
        prefix_chars += added_chars
        prefix_count += 1

    suffix_budget = available - prefix_chars
    suffix_parts: list[str] = []
    suffix_chars = 0
    for part in reversed(parts[prefix_count:]):
        added_chars = len(part) + (1 if suffix_parts else 0)
        if suffix_chars + added_chars > suffix_budget:
            break
        suffix_parts.append(part)
        suffix_chars += added_chars
    suffix_parts.reverse()

    prefix = "\n".join(prefix_parts)
    suffix = "\n".join(suffix_parts)
    if prefix or suffix:
        excerpt = f"{prefix}...{suffix}"
        if len(excerpt) <= limit:
            return excerpt

    prefix_length = available // 2
    suffix_length = available - prefix_length
    return f"{cleaned[:prefix_length].rstrip()}...{cleaned[-suffix_length:].lstrip()}"[
        :limit
    ]


def build_balanced_group_content(
    source_events: list[CollectedSourceEvent],
) -> str:
    contents_by_source: dict[str, list[str]] = {}
    for item in source_events:
        content = clean_text(item.event.content)
        if not content:
            continue
        source_key = item.source_file or item.person_name or item.draft_id
        values = contents_by_source.setdefault(source_key, [])
        if content not in values:
            values.append(content)

    balanced: list[str] = []
    position = 0
    while True:
        added = False
        for values in contents_by_source.values():
            if position >= len(values):
                continue
            balanced.append(values[position])
            added = True
        if not added:
            break
        position += 1
    return "\n\n".join(balanced)


def collected_events_are_similar(source_events: list[CollectedSourceEvent]) -> bool:
    if len(source_events) <= 1:
        return True
    for left_index, left in enumerate(source_events):
        for right in source_events[left_index + 1 :]:
            if not _events_are_deterministically_same(left.event, right.event):
                return False
    return True


def _events_are_deterministically_same(left: WorkEvent, right: WorkEvent) -> bool:
    left_workstream = _normalize_text(left.workstream_name)
    right_workstream = _normalize_text(right.workstream_name)
    if (
        left_workstream
        and right_workstream
        and left_workstream.casefold() != right_workstream.casefold()
    ):
        return False
    left_title = _normalize_text(left.title)
    right_title = _normalize_text(right.title)
    left_content = _normalize_text(left.content)
    right_content = _normalize_text(right.content)
    if left_title == right_title and left_content == right_content:
        return True
    if left_title != right_title:
        return False
    return left_content in right_content or right_content in left_content


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def repair_collected_merge_result(
    merge_result: CollectedMergeResult,
    source_events: list[CollectedSourceEvent],
    deterministic_groups: list[list[str]],
) -> tuple[CollectedMergeResult, list[str]]:
    expected = [item.draft_id for item in source_events]
    expected_set = set(expected)
    locked_members = {draft_id for group in deterministic_groups for draft_id in group}
    locked_map = {
        draft_id: tuple(group)
        for group in deterministic_groups
        for draft_id in group
    }
    seen: set[str] = set()
    groups: list[CollectedMergeGroup] = []
    duplicates: list[str] = []
    unknown: list[str] = []
    changed_locked: list[str] = []

    for group in merge_result.groups:
        original_draft_ids = list(group.draft_ids)
        normalized_ids: list[str] = []
        for draft_id in group.draft_ids:
            if draft_id not in expected_set:
                unknown.append(draft_id)
                continue
            if draft_id in seen:
                duplicates.append(draft_id)
                continue
            normalized_ids.append(draft_id)
        if not normalized_ids:
            continue
        locked_in_group = [draft_id for draft_id in normalized_ids if draft_id in locked_members]
        if locked_in_group:
            expected_locked_group = locked_map[locked_in_group[0]]
            if set(locked_in_group) != set(expected_locked_group):
                changed_locked.extend(locked_in_group)
                continue
            normalized_ids = list(expected_locked_group)
        for draft_id in normalized_ids:
            seen.add(draft_id)
        membership_unchanged = bool(
            len(normalized_ids) == len(original_draft_ids)
            and set(normalized_ids) == set(original_draft_ids)
        )
        groups.append(
            CollectedMergeGroup(
                group_id=group.group_id,
                draft_ids=normalized_ids,
                title=group.title,
                content=group.content,
                object_hint=group.object_hint,
                retention_reason=group.retention_reason,
                retention_detail=group.retention_detail,
                merge_owner_conflict=group.merge_owner_conflict,
                conflict_detail=group.conflict_detail,
                covered_draft_ids=(
                    None
                    if not membership_unchanged
                    or group.covered_draft_ids is None
                    else list(group.covered_draft_ids)
                ),
                fact_items=(list(group.fact_items) if membership_unchanged else []),
            )
        )

    source_by_id = {item.draft_id: item for item in source_events}
    missing = [draft_id for draft_id in expected if draft_id not in seen]
    for index, draft_id in enumerate(missing, start=1):
        source_event = source_by_id[draft_id]
        groups.append(
            CollectedMergeGroup(
                group_id=f"fallback-{index}",
                draft_ids=[draft_id],
                title=source_event.event.title,
                content=source_event.event.content,
                object_hint=source_event.event.object_hint,
                retention_reason=source_event.event.retention_reason,
                retention_detail=source_event.event.retention_detail,
            )
        )

    warnings: list[str] = []
    if missing or duplicates or unknown or changed_locked:
        details: list[str] = []
        if missing:
            details.append(f"missing={missing}")
        if duplicates:
            details.append(f"duplicates={sorted(set(duplicates))}")
        if unknown:
            details.append(f"unknown={sorted(set(unknown))}")
        if changed_locked:
            details.append(f"changed_locked={sorted(set(changed_locked))}")
        warnings.append("Collected merge groups were repaired: " + "; ".join(details))

    return CollectedMergeResult(groups=groups), warnings


def repair_collected_grouping_result(
    grouping_result: CollectedGroupingResult,
    source_events: list[CollectedSourceEvent],
    deterministic_groups: list[list[str]],
) -> tuple[CollectedGroupingResult, list[str]]:
    placeholder = CollectedMergeResult(
        groups=[
            CollectedMergeGroup(
                group_id=group.group_id,
                draft_ids=list(group.draft_ids),
                title="",
                content="",
            )
            for group in grouping_result.groups
        ]
    )
    repaired, warnings = repair_collected_merge_result(
        placeholder,
        source_events,
        deterministic_groups,
    )
    original_by_group_id = {
        group.group_id: group for group in grouping_result.groups
    }
    repaired_groups: list[CollectedGroupingGroup] = []
    discarded_summary_groups: list[str] = []
    missing_summary_groups: list[str] = []
    for group in repaired.groups:
        original = original_by_group_id.get(group.group_id)
        membership_unchanged = bool(
            original
            and len(original.draft_ids) == len(group.draft_ids)
            and set(original.draft_ids) == set(group.draft_ids)
        )
        if membership_unchanged and original is not None:
            is_singleton = len(group.draft_ids) == 1
            has_model_summary = all(
                clean_text(value)
                for value in (
                    original.summary_title,
                    original.summary_content,
                    original.summary_object_hint,
                )
            )
            summary_source = (
                "original_singleton"
                if is_singleton
                else "model"
                if has_model_summary
                else "balanced_fallback"
            )
            if not is_singleton and not has_model_summary:
                missing_summary_groups.append(group.group_id)
            repaired_groups.append(
                replace(
                    original,
                    draft_ids=list(group.draft_ids),
                    summary_source=summary_source,
                    was_repaired=(
                        original.was_repaired
                        or (not is_singleton and not has_model_summary)
                    ),
                )
            )
            continue

        if original and any(
            clean_text(value)
            for value in (
                original.summary_title,
                original.summary_content,
                original.summary_object_hint,
            )
        ):
            discarded_summary_groups.append(group.group_id)
        repaired_groups.append(
            CollectedGroupingGroup(
                group_id=group.group_id,
                draft_ids=list(group.draft_ids),
                summary_source=(
                    "original_singleton"
                    if len(group.draft_ids) == 1
                    else "balanced_fallback"
                ),
                was_repaired=True,
            )
        )

    if discarded_summary_groups:
        warnings.append(
            "Discarded collected candidate summaries after group membership repair: "
            + ", ".join(discarded_summary_groups)
        )
    if missing_summary_groups:
        warnings.append(
            "Used balanced candidate content because model summaries were missing: "
            + ", ".join(missing_summary_groups)
        )
    return CollectedGroupingResult(groups=repaired_groups), warnings


def collected_grouping_partition_error(
    grouping_result: CollectedGroupingResult,
    expected_draft_ids: list[str],
) -> str:
    expected = set(expected_draft_ids)
    flattened = [
        draft_id
        for group in grouping_result.groups
        for draft_id in group.draft_ids
    ]
    counts = Counter(flattened)
    missing = sorted(expected.difference(counts))
    unknown = sorted(set(counts).difference(expected))
    duplicates = sorted(
        draft_id for draft_id, count in counts.items() if count > 1
    )
    details: list[str] = []
    if missing:
        details.append(f"missing={missing}")
    if unknown:
        details.append(f"unknown={unknown}")
    if duplicates:
        details.append(f"duplicates={duplicates}")
    return "; ".join(details)


def _build_singleton_collected_group(
    source_event: CollectedSourceEvent,
    index: int,
) -> CollectedMergeGroup:
    event = source_event.event
    return CollectedMergeGroup(
        group_id=f"singleton-{index}",
        draft_ids=[source_event.draft_id],
        title=event.title,
        content=event.content,
        object_hint=event.object_hint,
        retention_reason=event.retention_reason,
        retention_detail=event.retention_detail,
    )


def enforce_collected_workstream_boundaries(
    merge_result: CollectedMergeResult,
    source_events: list[CollectedSourceEvent],
) -> tuple[CollectedMergeResult, list[str]]:
    source_by_id = {item.draft_id: item for item in source_events}
    groups: list[CollectedMergeGroup] = []
    warnings: list[str] = []

    for group in merge_result.groups:
        items = [
            source_by_id[draft_id]
            for draft_id in group.draft_ids
            if draft_id in source_by_id
        ]
        named_groups: dict[str, list[CollectedSourceEvent]] = {}
        unnamed_items: list[CollectedSourceEvent] = []
        for item in items:
            name = clean_text(item.event.workstream_name)
            if not name:
                unnamed_items.append(item)
                continue
            normalized_name = "".join(name.casefold().split())
            named_groups.setdefault(normalized_name, []).append(item)

        if len(named_groups) <= 1:
            groups.append(group)
            continue

        if _named_workstream_groups_share_thread_evidence(named_groups):
            groups.append(group)
            warnings.append(
                "Allowed collected merge across different named workstreams because "
                f"shared conversation or message evidence connects the group: {group.group_id}."
            )
            continue

        partitions = [*named_groups.values(), *([item] for item in unnamed_items)]
        for index, partition in enumerate(partitions, start=1):
            groups.append(
                _build_boundary_fallback_group(
                    group,
                    partition,
                    partition_index=index,
                )
            )
        warnings.append(
            "Split collected merge group because different named workstreams cannot merge: "
            f"{group.group_id}."
        )

    return CollectedMergeResult(groups=groups), warnings


def _named_workstream_groups_share_thread_evidence(
    named_groups: dict[str, list[CollectedSourceEvent]],
) -> bool:
    names = list(named_groups)
    connected: dict[str, set[str]] = {name: set() for name in names}
    for left_index, left_name in enumerate(names):
        for right_name in names[left_index + 1 :]:
            if any(
                _events_share_thread_evidence(left.event, right.event)
                for left in named_groups[left_name]
                for right in named_groups[right_name]
            ):
                connected[left_name].add(right_name)
                connected[right_name].add(left_name)

    seen = {names[0]}
    pending = [names[0]]
    while pending:
        current = pending.pop()
        for neighbor in connected[current]:
            if neighbor in seen:
                continue
            seen.add(neighbor)
            pending.append(neighbor)
    return len(seen) == len(names)


def _events_share_thread_evidence(left: WorkEvent, right: WorkEvent) -> bool:
    left_messages = set(left.evidence_fingerprints)
    right_messages = set(right.evidence_fingerprints)
    left_conversations = set(left.conversation_fingerprints)
    right_conversations = set(right.conversation_fingerprints)
    return bool(
        left_messages.intersection(right_messages)
        or left_conversations.intersection(right_conversations)
    )


def _build_boundary_fallback_group(
    original_group: CollectedMergeGroup,
    items: list[CollectedSourceEvent],
    *,
    partition_index: int,
) -> CollectedMergeGroup:
    return CollectedMergeGroup(
        group_id=f"{original_group.group_id}-workstream-{partition_index}",
        draft_ids=[item.draft_id for item in items],
        title=choose_preferred_text([item.event.title for item in items]),
        content=merge_content_texts([item.event.content for item in items]),
        object_hint=choose_preferred_text([item.event.object_hint for item in items]),
        retention_reason=choose_preferred_text(
            [
                item.event.retention_reason
                for item in items
                if item.event.retention_reason in RETENTION_REASONS
            ]
        ),
        retention_detail=derive_collected_merge_retention_detail(items),
    )


def _merge_file_links(source_events: list[CollectedSourceEvent]) -> list[EventFileLink]:
    seen: set[tuple[str, str]] = set()
    links: list[EventFileLink] = []
    for source_event in source_events:
        for link in source_event.event.file_links:
            key = (link.url, link.title)
            if key in seen:
                continue
            seen.add(key)
            links.append(link)
    return links


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        stripped = value.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        result.append(stripped)
    return result


def _estimate_prepared_model_prompt_tokens(prompt: str) -> int:
    stripped = prompt.strip()
    if not stripped.endswith("/no_think"):
        stripped = f"{stripped}\n/no_think"
    return estimate_text_tokens(stripped)


def _sort_self_relations(
    values: list[str],
    *,
    config: RuntimeConfig,
) -> list[str]:
    deduped = _dedupe(values)
    configured_order = [item.key for item in config.self_relation_types]
    configured = [value for value in configured_order if value in deduped]
    return [*configured, *(value for value in deduped if value not in configured)]


def _deliver_markdown_to_self(
    delivery_channel: Any,
    *,
    self_identity: SelfIdentity,
    markdown_path: Path,
) -> tuple[str, str, str]:
    try:
        status, target = delivery_channel.deliver_to_self(
            self_identity=self_identity,
            markdown_path=markdown_path,
        )
        return status, target, ""
    except DeliveryError as exc:
        return "failed", self_identity.open_id, str(exc)
