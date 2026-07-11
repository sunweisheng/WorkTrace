from __future__ import annotations

import json
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Sequence

from .config import RuntimeConfig
from .constants import DailyRunStatus
from .delivery.feishu_cli import FeishuCliSelfDelivery
from .errors import AnalyzerProtocolError, DeliveryError, StoreWriteError
from .factories import AnalyzerFactory
from .analyzers.prompts import build_collected_merge_prompt
from .models import (
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
from .pipeline.sensitive_filter import filter_work_events
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
from .utils.hashing import stable_event_id
from .utils.json_io import dump_json
from .utils.text import choose_preferred_text, clean_text


@dataclass
class CollectedMergeRunner:
    config: RuntimeConfig
    analyzer: Any | None = None
    cwd: Path | None = None
    command_runner: Any | None = None
    delivery_channel: Any | None = None
    self_identity_resolver: Any | None = None

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
        self.store = MarkdownEventStore(config=self.config)
        self._collected_merge_trace_dir: Path | None = None
        self._collected_merge_trace_steps: list[dict[str, Any]] = []
        self._collected_merge_trace_call_index = 0

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
            warning_messages=warning_messages,
            self_delivery_status=summarize_self_delivery_status(outputs),
            self_delivery_target=first_output.self_delivery_target,
            self_delivery_error="; ".join(delivery_errors),
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
        skipped_file_count = 0

        source_events, source_file_count, skipped_file_count, read_warnings = (
            self._read_source_events(
                target_date,
                input_dir,
                output_path=output_path,
                ignored_subdirectories=ignored_subdirectories,
            )
        )
        warning_messages.extend(read_warnings)
        source_events, source_filter_warnings = self._filter_source_events(source_events)
        warning_messages.extend(source_filter_warnings)
        source_events, retention_source_warnings = self._filter_retained_source_events(
            source_events,
        )
        warning_messages.extend(retention_source_warnings)
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
                return CollectedMergeOutput(
                    input_dir=str(input_dir.resolve()),
                    output_path=None,
                    source_file_count=source_file_count,
                    source_event_count=len(source_events),
                    merged_event_count=0,
                    skipped_file_count=skipped_file_count,
                    warning_messages=[*warning_messages, str(exc)],
                )
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
            raise StoreWriteError(f"Failed to write merged markdown: {output_path}") from exc

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
            target_date=target_date,
            input_dir=input_dir,
            output_path=output_path,
            source_file_count=source_file_count,
            source_event_count=len(source_events),
            merged_event_count=len(merged_events),
            warning_messages=warning_messages,
        )

        return CollectedMergeOutput(
            input_dir=str(input_dir.resolve()),
            output_path=str(output_path.resolve()),
            source_file_count=source_file_count,
            source_event_count=len(source_events),
            merged_event_count=len(merged_events),
            skipped_file_count=skipped_file_count,
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
        )
        merge_result, repair_warnings = repair_collected_merge_result(
            merge_result,
            source_events,
            deterministic_groups,
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
        merged_events_after_sensitive, sensitive_warnings = self._filter_events(
            merged_events_before_filters
        )
        retained_events, retention_warnings = filter_retained_work_events(
            merged_events_after_sensitive,
        )
        self._record_collected_merge_trace_final(
            repaired_result=merge_result,
            merged_events_before_filters=merged_events_before_filters,
            merged_events_after_sensitive=merged_events_after_sensitive,
            retained_events=retained_events,
            repair_warnings=repair_warnings,
            metadata_warnings=metadata_warnings,
            sensitive_warnings=sensitive_warnings,
            retention_warnings=retention_warnings,
        )
        return retained_events, [
            *retry_warnings,
            *repair_warnings,
            *metadata_warnings,
            *sensitive_warnings,
            *retention_warnings,
        ]

    def _invoke_collected_merge_with_retry(
        self,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        deterministic_groups: list[list[str]],
    ) -> tuple[CollectedMergeResult, list[str]]:
        warnings: list[str] = []
        retry_limit = self.config.collected_merge_missing_field_retry_limit
        attempt_index = 0
        while True:
            attempt_index += 1
            merge_result = self.analyzer.merge_collected_events(
                target_date,
                source_events,
                deterministic_groups,
            )
            missing_summary = collected_merge_missing_field_summary(merge_result)
            self._record_collected_merge_trace_attempt(
                target_date=target_date,
                source_events=source_events,
                deterministic_groups=deterministic_groups,
                merge_result=merge_result,
                attempt_index=attempt_index,
                missing_summary=missing_summary,
            )
            if not self._should_retry_collected_merge_missing_fields(
                merge_result,
                missing_summary,
            ):
                return merge_result, warnings
            if attempt_index > retry_limit:
                return merge_result, warnings
            warning = (
                "Retrying collected merge because required fields were missing: "
                f"{format_collected_merge_missing_field_summary(missing_summary)}"
            )
            warnings.append(warning)

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
                )
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
    ) -> tuple[list[CollectedSourceEvent], int, int, list[str]]:
        warnings: list[str] = []
        source_events: list[CollectedSourceEvent] = []
        source_file_count = 0
        skipped_file_count = 0
        ignored_subdirectories = ignored_subdirectories or set()

        if not input_dir.exists():
            return [], 0, 0, [f"Input directory does not exist: {input_dir}"]

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
                continue
            try:
                day_doc = self.store.parse_day_document(path.read_text(encoding="utf-8"))
            except (OSError, StoreWriteError, KeyError, ValueError) as exc:
                skipped_file_count += 1
                warnings.append(f"Skipped invalid source markdown: {path.name} ({exc})")
                continue
            if day_doc.date != target_date:
                warnings.append(
                    f"Source markdown date mismatch: {path.name} ({day_doc.date})"
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

        return source_events, source_file_count, skipped_file_count, warnings

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
    ) -> tuple[list[CollectedSourceEvent], list[str]]:
        kept_events, warnings = filter_work_events(
            [source_event.event for source_event in source_events],
            self.config,
        )
        kept_ids = {id(event) for event in kept_events}
        return [
            source_event
            for source_event in source_events
            if id(source_event.event) in kept_ids
        ], warnings

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
                )
            )
        return events

    def _filter_events(
        self,
        events: list[WorkEvent],
    ) -> tuple[list[WorkEvent], list[str]]:
        return filter_work_events(events, self.config)

    def _start_collected_merge_trace(self, target_date: str, input_dir: Path) -> None:
        if not self.config.collected_merge_trace_enabled:
            self._collected_merge_trace_dir = None
            self._collected_merge_trace_steps = []
            self._collected_merge_trace_call_index = 0
            return
        root = self.config.collected_merge_trace_root
        trace_root = root if root.is_absolute() else self.cwd / root
        date_dir = trace_root / target_date
        if input_dir.name != target_date.split("-")[-1]:
            date_dir = date_dir / input_dir.name
        date_dir.mkdir(parents=True, exist_ok=True)
        self._collected_merge_trace_dir = date_dir
        self._collected_merge_trace_steps = []
        self._collected_merge_trace_call_index = 0

    def _record_collected_merge_trace_attempt(
        self,
        *,
        target_date: str,
        source_events: list[CollectedSourceEvent],
        deterministic_groups: list[list[str]],
        merge_result: CollectedMergeResult,
        attempt_index: int,
        missing_summary: dict[str, int],
    ) -> None:
        if self._collected_merge_trace_dir is None:
            return
        self._collected_merge_trace_call_index += 1
        prompt_chars = self._count_collected_merge_prompt_chars(
            target_date,
            source_events,
            deterministic_groups,
        )
        step = {
            "step_index": self._collected_merge_trace_call_index,
            "attempt_index": attempt_index,
            "prompt_chars": prompt_chars,
            "input": collected_merge_source_metrics(source_events),
            "raw_group_count": len(merge_result.groups),
            "raw_group_metrics": collected_merge_group_metrics(merge_result),
            "missing_required_field_summary": missing_summary,
            "raw_result": merge_result.to_dict(),
        }
        self._collected_merge_trace_steps.append(step)
        self._write_collected_merge_trace_step(step)

    def _record_collected_merge_trace_final(
        self,
        *,
        repaired_result: CollectedMergeResult,
        merged_events_before_filters: list[WorkEvent],
        merged_events_after_sensitive: list[WorkEvent],
        retained_events: list[WorkEvent],
        repair_warnings: list[str],
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

    def _write_collected_merge_trace_summary(
        self,
        *,
        target_date: str,
        input_dir: Path,
        output_path: Path,
        source_file_count: int,
        source_event_count: int,
        merged_event_count: int,
        warning_messages: list[str],
    ) -> None:
        if self._collected_merge_trace_dir is None:
            return
        summary = {
            "target_date": target_date,
            "input_dir": str(input_dir.resolve()),
            "output_path": str(output_path.resolve()),
            "source_file_count": source_file_count,
            "source_event_count": source_event_count,
            "merged_event_count": merged_event_count,
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
        f"- Source files: {summary['source_file_count']}",
        f"- Source events: {summary['source_event_count']}",
        f"- Merged events: {summary['merged_event_count']}",
        f"- Output: `{summary['output_path']}`",
        "",
        "## Step Metrics",
        "",
        "| Step | Attempt | Prompt chars | Input events | Synthetic inputs | Raw groups | Missing detail raw | Filled metadata | Retained events | Dropped by retention |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for step in summary["steps"]:
        missing_detail = step.get("missing_required_field_summary", {}).get(
            "retention_detail",
            0,
        )
        metadata_warnings = len(step.get("metadata_warnings", []))
        retained_events = step.get("retained_metrics", {}).get("event_count", "")
        dropped = len(step.get("dropped_by_retention", []))
        lines.append(
            "| {step} | {attempt} | {prompt} | {input_events} | {synthetic} | "
            "{raw_groups} | {missing_detail} | {metadata_warnings} | "
            "{retained_events} | {dropped} |".format(
                step=step.get("step_index", ""),
                attempt=step.get("attempt_index", ""),
                prompt=step.get("prompt_chars", ""),
                input_events=step.get("input", {}).get("event_count", ""),
                synthetic=step.get("input", {}).get("synthetic_event_count", ""),
                raw_groups=step.get("raw_group_count", ""),
                missing_detail=missing_detail,
                metadata_warnings=metadata_warnings,
                retained_events=retained_events,
                dropped=dropped,
            )
        )
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


def collected_events_are_similar(source_events: list[CollectedSourceEvent]) -> bool:
    if len(source_events) <= 1:
        return True
    for left_index, left in enumerate(source_events):
        for right in source_events[left_index + 1 :]:
            if not _events_are_deterministically_same(left.event, right.event):
                return False
    return True


def _events_are_deterministically_same(left: WorkEvent, right: WorkEvent) -> bool:
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
