from __future__ import annotations

import json
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .config import RuntimeConfig
from .constants import DailyRunStatus
from .errors import AnalyzerProtocolError, StoreWriteError
from .factories import AnalyzerFactory
from .models import (
    CollectedMergeGroup,
    CollectedMergeResult,
    CollectedMergeRunResult,
    CollectedSourceEvent,
    DayDocument,
    EventFileLink,
    MergedEventDraft,
    WorkEvent,
)
from .pipeline.sensitive_filter import filter_sensitive_merged_drafts
from .pipeline.retention_filter import filter_retained_work_events
from .stores.markdown import MarkdownEventStore
from .utils.dates import now_iso
from .utils.hashing import stable_event_id
from .utils.json_io import load_json_object

MERGE_DELIVERY_CONFIG = "config/merge_delivery.local.json"
_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-(?P<name>.+)\.md$")


@dataclass
class CollectedMergeRunner:
    config: RuntimeConfig
    analyzer: Any | None = None
    cwd: Path | None = None
    command_runner: Any | None = None

    def __post_init__(self) -> None:
        if self.cwd is None:
            self.cwd = Path.cwd()
        if self.analyzer is None:
            self.analyzer = AnalyzerFactory.create_default(self.config)
        if self.command_runner is None:
            self.command_runner = self._run_command
        self.store = MarkdownEventStore(config=self.config)

    def run(self, target_date: str) -> CollectedMergeRunResult:
        input_dir = self.build_input_dir(target_date)
        output_path = input_dir / "_merged.md"
        warning_messages: list[str] = []
        skipped_file_count = 0

        source_events, source_file_count, skipped_file_count, read_warnings = (
            self._read_source_events(target_date, input_dir)
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
                return CollectedMergeRunResult(
                    status=DailyRunStatus.FAILED.value,
                    target_date=target_date,
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

        upload_status, upload_target, upload_error = self._upload_if_configured(
            target_date,
            output_path,
        )
        if upload_error:
            warning_messages.append(upload_error)

        status = (
            DailyRunStatus.SUCCESS_WITH_WARNINGS.value
            if warning_messages
            else DailyRunStatus.SUCCESS.value
        )
        return CollectedMergeRunResult(
            status=status,
            target_date=target_date,
            input_dir=str(input_dir.resolve()),
            output_path=str(output_path.resolve()),
            source_file_count=source_file_count,
            source_event_count=len(source_events),
            merged_event_count=len(merged_events),
            skipped_file_count=skipped_file_count,
            warning_messages=warning_messages,
            upload_status=upload_status,
            upload_target=upload_target,
            upload_error=upload_error,
        )

    def build_input_dir(self, target_date: str) -> Path:
        year, month, day = target_date.split("-")
        return self.cwd / "merge_inbox" / year / month / day

    def _read_source_events(
        self,
        target_date: str,
        input_dir: Path,
    ) -> tuple[list[CollectedSourceEvent], int, int, list[str]]:
        warnings: list[str] = []
        source_events: list[CollectedSourceEvent] = []
        source_file_count = 0
        skipped_file_count = 0

        if not input_dir.exists():
            return [], 0, 0, [f"Input directory does not exist: {input_dir}"]

        for path in sorted(input_dir.iterdir()):
            if should_skip_input_file(path):
                continue
            if path.suffix != ".md":
                skipped_file_count += 1
                continue
            source_file_count += 1
            person_name = extract_person_name_from_filename(path.name)
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

    def _upload_if_configured(
        self,
        target_date: str,
        output_path: Path,
    ) -> tuple[str, str, str]:
        try:
            delivery_config = load_merge_delivery_config(self.cwd)
            folder_url = delivery_config.get("feishu_drive_folder_url", "").strip()
            if not folder_url:
                return ("skipped", "", "")

            year, month, day = target_date.split("-")
            target_folder = ensure_drive_folder_path(
                folder_url,
                [year, month, day],
                command_runner=self.command_runner,
                cwd=self.cwd,
            )
            upload_markdown_to_drive(
                output_path,
                target_folder,
                command_runner=self.command_runner,
                cwd=self.cwd,
            )
            return ("success", target_folder, "")
        except Exception as exc:  # noqa: BLE001 - upload errors are non-fatal warnings.
            return ("failed", "", f"Failed to upload merged markdown: {exc}")

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


def extract_person_name_from_filename(filename: str) -> str:
    match = _FILENAME_RE.match(filename)
    if not match:
        return ""
    return match.group("name").strip()


def should_skip_input_file(path: Path) -> bool:
    return path.name == "_merged.md" or path.name.startswith(".") or path.is_dir()


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


def load_merge_delivery_config(cwd: Path) -> dict[str, str]:
    path = cwd / MERGE_DELIVERY_CONFIG
    try:
        payload = load_json_object(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(payload.get("feishu_drive_folder_url", ""), str):
        raise ValueError(f"Invalid merge delivery config: {path}")
    return {"feishu_drive_folder_url": payload.get("feishu_drive_folder_url", "")}


def ensure_drive_folder_path(
    root_folder_url: str,
    parts: list[str],
    *,
    command_runner: Any,
    cwd: Path,
) -> str:
    current = root_folder_url
    for part in parts:
        result = command_runner(
            (
                "lark-cli",
                "drive",
                "+folders-create",
                "--as",
                "user",
                "--parent-folder",
                current,
                "--name",
                part,
            ),
            cwd=cwd,
        )
        if getattr(result, "returncode", 1) != 0:
            stderr = (getattr(result, "stderr", "") or "").strip()
            raise RuntimeError(stderr or f"Failed to create Drive folder: {part}")
        current = _extract_drive_target(getattr(result, "stdout", "") or "") or current
    return current


def upload_markdown_to_drive(
    markdown_path: Path,
    target_folder: str,
    *,
    command_runner: Any,
    cwd: Path,
) -> None:
    result = command_runner(
        (
            "lark-cli",
            "drive",
            "+upload",
            "--as",
            "user",
            "--folder",
            target_folder,
            "--file",
            str(markdown_path),
        ),
        cwd=cwd,
    )
    if getattr(result, "returncode", 1) != 0:
        stderr = (getattr(result, "stderr", "") or "").strip()
        raise RuntimeError(stderr or "Failed to upload markdown to Drive.")


def _extract_drive_target(stdout: str) -> str:
    stripped = stdout.strip()
    if not stripped:
        return ""
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped
    for key in ("url", "folder_url", "token", "folder_token"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("url", "folder_url", "token", "folder_token"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return stripped


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
