from __future__ import annotations

import json
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
from .analyzers.prompts import (
    build_collected_grouping_prompt,
    build_collected_merge_prompt,
    build_collected_render_prompt,
)
from .models import (
    CollectedGroupingGroup,
    CollectedGroupingResult,
    CollectedMergeGroup,
    CollectedMergeOutput,
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
                output = CollectedMergeOutput(
                    input_dir=str(input_dir.resolve()),
                    output_path=None,
                    source_file_count=source_file_count,
                    source_event_count=len(source_events),
                    merged_event_count=0,
                    skipped_file_count=skipped_file_count,
                    partial_file_count=partial_file_count,
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
                )
                return output
        else:
            warning_messages.append("No valid source events found.")

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
            )
            return CollectedMergeOutput(
                input_dir=str(input_dir.resolve()),
                output_path=None,
                source_file_count=source_file_count,
                source_event_count=len(source_events),
                merged_event_count=0,
                skipped_file_count=skipped_file_count,
                partial_file_count=partial_file_count,
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
        )

        return CollectedMergeOutput(
            input_dir=str(input_dir.resolve()),
            output_path=str(output_path.resolve()),
            source_file_count=source_file_count,
            source_event_count=len(source_events),
            merged_event_count=len(merged_events),
            skipped_file_count=skipped_file_count,
            partial_file_count=partial_file_count,
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
            *retry_warnings,
            *final_warnings,
        ]

    def _render_collected_multi_groups(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        groups: list[CollectedGroupingGroup],
    ) -> tuple[list[CollectedMergeGroup], list[str]]:
        threshold = self.config.collected_merge_prompt_char_threshold
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
            candidate_chars = self._count_collected_render_prompt_chars(
                target_date,
                candidate_events,
                [list(item.draft_ids) for item in candidate_groups],
            )
            if current and candidate_chars > threshold:
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
            batch_chars = self._count_collected_render_prompt_chars(
                target_date,
                batch_events,
                batch_locked,
            )
            if len(batch_groups) == 1 and batch_chars > threshold:
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
                f"batches={len(batches)} threshold={threshold}.",
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
        threshold = self.config.collected_merge_prompt_char_threshold
        locked_group = [item.draft_id for item in source_events]
        prompt_chars = self._count_collected_render_prompt_chars(
            target_date,
            source_events,
            [locked_group],
        )
        if prompt_chars <= threshold or len(source_events) <= 1 or depth >= 3:
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
                replace(rendered, draft_ids=list(group.draft_ids)),
                [*retry_warnings, *repair_warnings],
            )

        chunks: list[list[CollectedSourceEvent]] = []
        current: list[CollectedSourceEvent] = []
        for item in source_events:
            candidate = [*current, item]
            candidate_chars = self._count_collected_render_prompt_chars(
                target_date,
                candidate,
                [[event.draft_id for event in candidate]],
            )
            if current and candidate_chars > threshold:
                chunks.append(current)
                current = []
            current.append(item)
        if current:
            chunks.append(current)

        summary_events: list[CollectedSourceEvent] = []
        warnings = [
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
        return replace(rendered, draft_ids=list(group.draft_ids)), warnings

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
        prompt_chars = self._count_collected_merge_prompt_chars(
            target_date,
            source_events,
            deterministic_groups,
        )
        source_groups = self._group_source_events_for_rolling(
            target_date,
            source_events,
        )
        threshold = self.config.collected_merge_prompt_char_threshold
        if prompt_chars <= threshold or len(source_groups) < 3:
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
            f"prompt_chars={prompt_chars} threshold={threshold} calls={call_count}"
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
        prompt_chars = len(
            build_collected_grouping_prompt(
                target_date,
                source_events,
                deterministic_groups,
                config=self.config,
            )
        )
        threshold = self.config.collected_merge_prompt_char_threshold
        if prompt_chars <= threshold or len(source_events) <= 1:
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
            f"prompt_chars={prompt_chars} threshold={threshold} batches={len(batches)}."
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
            batch_result, batch_warnings = self._invoke_collected_grouping_once(
                target_date,
                batch_events,
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
            warnings.extend([*batch_warnings, *batch_repair_warnings])

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
        threshold = self.config.collected_merge_prompt_char_threshold
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
            candidate_chars = len(
                build_collected_grouping_prompt(
                    target_date,
                    candidate,
                    candidate_deterministic,
                    config=self.config,
                )
            )
            if current and candidate_chars > threshold:
                batches.append(current)
                current = []

            component_chars = len(
                build_collected_grouping_prompt(
                    target_date,
                    component,
                    [
                        list(group)
                        for group in deterministic_groups
                        if set(group).issubset(
                            {item.draft_id for item in component}
                        )
                    ],
                    config=self.config,
                )
            )
            if component_chars <= threshold:
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
                candidate_chars = len(
                    build_collected_grouping_prompt(
                        target_date,
                        candidate,
                        [],
                        config=self.config,
                    )
                )
                if current and candidate_chars > threshold:
                    batches.append(current)
                    current = []
                current.extend(unit)

        if current:
            batches.append(current)
        return batches or [list(source_events)]

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
            self._record_collected_merge_trace_success(
                step_index=trace_step_index,
                merge_result=merge_result,
                missing_summary=missing_summary,
            )
            if not self._should_retry_collected_merge_missing_fields(
                merge_result,
                missing_summary,
            ):
                return merge_result, warnings
            if (
                missing_field_retry_count
                >= self.config.collected_merge_missing_field_retry_limit
            ):
                return merge_result, warnings
            missing_field_retry_count += 1
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
                integrated_content = merge_content_texts(
                    [content, *(item.event.content for item in items)]
                )
                if integrated_content != content:
                    content = integrated_content
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

    def _count_collected_merge_prompt_chars(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        deterministic_groups: list[list[str]],
    ) -> int:
        return len(
            build_collected_merge_prompt(
                target_date,
                source_events,
                deterministic_groups,
                config=self.config,
            )
        )

    def _count_collected_render_prompt_chars(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        deterministic_groups: list[list[str]],
    ) -> int:
        return len(
            build_collected_render_prompt(
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
                self._count_collected_merge_prompt_chars(
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

        warning = (
            "No merge-owner personal event markdown matched current user "
            f"'{owner_name}' in directory: {input_dir.resolve()}; "
            "falling back to standard collected merge."
        )
        return marked_events, [warning]

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
    ) -> int:
        if self._collected_merge_trace_dir is None:
            return 0
        self._collected_merge_trace_call_index += 1
        prompt = (
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
            "prompt_file": f"step-{self._collected_merge_trace_call_index:03d}-prompt.txt",
            "input": collected_merge_source_metrics(source_events),
            "input_events": [item.to_dict() for item in source_events],
            "deterministic_groups": [list(group) for group in deterministic_groups],
        }
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
    ) -> None:
        step = self._collected_merge_trace_step(step_index)
        if step is None:
            return
        step.update(
            {
                "status": "success",
                "raw_group_count": len(grouping_result.groups),
                "raw_result": grouping_result.to_dict(),
            }
        )
        self._write_collected_merge_trace_step(step)

    def _record_collected_merge_trace_success(
        self,
        *,
        step_index: int,
        merge_result: CollectedMergeResult,
        missing_summary: dict[str, int],
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
        "## Step Metrics",
        "",
        "| Step | Stage | Status | Batch/Rolling step | Attempt | Retry reason | Prompt chars | Input events | Raw groups | Retained events | Error |",
        "|---:|---|---|---:|---:|---|---:|---:|---:|---:|---|",
    ]
    for step in summary["steps"]:
        retained_events = step.get("retained_metrics", {}).get("event_count", "")
        error_summary = str(step.get("error", {}).get("summary", "")).replace("|", "\\|")
        lines.append(
            "| {step} | {stage} | {status} | {rolling} | {attempt} | {retry_reason} | "
            "{prompt} | {input_events} | {raw_groups} | {retained_events} | "
            "{error} |".format(
                step=step.get("step_index", ""),
                stage=step.get("stage", ""),
                status=step.get("status", ""),
                rolling=step.get("rolling_step_index", ""),
                attempt=step.get("attempt_index", ""),
                retry_reason=step.get("retry_reason", ""),
                prompt=step.get("prompt_chars", ""),
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
        synthetic_id = f"__grouping_summary_{depth}_{index}"
        source_ids_by_synthetic[synthetic_id] = list(group.draft_ids)
        summaries.append(
            CollectedSourceEvent(
                draft_id=synthetic_id,
                person_name="grouping-summary",
                source_file=f"__grouping_summary_{depth}.md",
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
                    title=choose_preferred_text(
                        [item.event.title for item in items]
                    ),
                    content=merge_content_texts(
                        [item.event.content for item in items]
                    ),
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
                    object_hint=choose_preferred_text(
                        [item.event.object_hint for item in items]
                    ),
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
    return (
        CollectedGroupingResult(
            groups=[
                CollectedGroupingGroup(
                    group_id=group.group_id,
                    draft_ids=list(group.draft_ids),
                )
                for group in repaired.groups
            ]
        ),
        warnings,
    )


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
