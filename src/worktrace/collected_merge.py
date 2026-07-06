from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .config import RuntimeConfig
from .constants import DailyRunStatus
from .delivery.feishu_cli import FeishuCliSelfDelivery
from .errors import AnalyzerProtocolError, DeliveryError, StoreWriteError
from .factories import AnalyzerFactory
from .models import (
    CollectedMergeGroup,
    CollectedMergeOutput,
    CollectedMergeResult,
    CollectedMergeRunResult,
    CollectedSourceEvent,
    DayDocument,
    EventFileLink,
    MergedEventDraft,
    SelfIdentity,
    WorkEvent,
)
from .pipeline.sensitive_filter import filter_sensitive_merged_drafts
from .pipeline.retention_filter import filter_retained_work_events
from .stores.markdown import MarkdownEventStore
from .utils.dates import now_iso
from .utils.filenames import (
    build_merged_markdown_filename,
    parse_worktrace_markdown_filename,
)
from .utils.hashing import stable_event_id


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
        warning_messages: list[str] = []
        skipped_file_count = 0

        source_events, source_file_count, skipped_file_count, read_warnings = (
            self._read_source_events(
                target_date,
                input_dir,
                ignored_subdirectories=ignored_subdirectories,
            )
        )
        warning_messages.extend(read_warnings)
        source_events, retention_source_warnings = self._filter_retained_source_events(
            source_events,
        )
        warning_messages.extend(retention_source_warnings)

        merged_events: list[WorkEvent] = []
        if source_events:
            deterministic_groups, deterministic_warnings = (
                self._build_deterministic_groups(source_events)
            )
            warning_messages.extend(deterministic_warnings)
            try:
                merge_result = self.analyzer.merge_collected_events(
                    target_date,
                    source_events,
                    deterministic_groups,
                )
                merge_result, repair_warnings = repair_collected_merge_result(
                    merge_result,
                    source_events,
                    deterministic_groups,
                )
                warning_messages.extend(repair_warnings)
                merged_events = self._materialize_events(
                    target_date,
                    source_events,
                    merge_result,
                )
                merged_events, sensitive_warnings = self._filter_sensitive_events(
                    target_date,
                    merged_events,
                )
                warning_messages.extend(sensitive_warnings)
                merged_events, retention_merged_warnings = filter_retained_work_events(
                    merged_events,
                )
                warning_messages.extend(retention_merged_warnings)
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

    def build_input_dir(self, target_date: str) -> Path:
        year, month, day = target_date.split("-")
        return self.cwd / "merge_inbox" / year / month / day

    def _read_source_events(
        self,
        target_date: str,
        input_dir: Path,
        *,
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
            if should_skip_input_file(path):
                continue
            if path.suffix != ".md":
                skipped_file_count += 1
                continue
            source_file_count += 1
            person_name = extract_person_name_from_filename(path.name, target_date=target_date)
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
            source_people = _dedupe([item.person_name for item in items])
            source_event_ids = _dedupe([item.event.event_id for item in items])
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

    def _filter_sensitive_events(
        self,
        target_date: str,
        events: list[WorkEvent],
    ) -> tuple[list[WorkEvent], list[str]]:
        drafts = [
            MergedEventDraft(
                date=target_date,
                topic=event.title,
                content=event.content,
                object_hint=event.object_hint,
                retention_reason=event.retention_reason,
                retention_detail=event.retention_detail,
                source_message_ids=[event.event_id],
                source_conversation_ids=[],
            )
            for event in events
        ]
        kept_drafts, warnings = filter_sensitive_merged_drafts(drafts, self.config)
        kept_keys = {(draft.topic, draft.content) for draft in kept_drafts}
        return [
            event for event in events if (event.title, event.content) in kept_keys
        ], warnings

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
    if parsed.suffix != ".md" or parsed.is_merged:
        return ""
    if not parsed.target_date or not parsed.owner_name:
        return ""
    if target_date and parsed.target_date != target_date:
        return ""
    return parsed.owner_name.strip()


def should_skip_input_file(path: Path) -> bool:
    if path.name == "_merged.md" or path.name.startswith(".") or path.is_dir():
        return True
    parsed = parse_worktrace_markdown_filename(path.name)
    return parsed.suffix == ".md" and parsed.is_merged


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
