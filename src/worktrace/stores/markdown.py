from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .. import __version__
from ..config import RuntimeConfig
from ..errors import StoreWriteError
from ..models import DayDocument, EventFileLink, StoreWriteResult, WorkEvent
from ..utils.dates import now_iso
from ..utils.hashing import (
    evidence_fingerprint,
    event_content_fingerprint,
    file_key_from_attachment_id,
    file_key_from_url,
    is_sha256_fingerprint,
    stable_event_id,
)
from ..utils.filenames import (
    build_personal_markdown_filename,
    parse_worktrace_markdown_filename,
)
from ..utils.text import normalize_sentence_final_ma
from .base import EventStore


SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "code",
    "key",
    "passwd",
    "password",
    "secret",
    "signature",
    "sign",
    "token",
}

RETENTION_REASON_LABELS = {
    "deliverable_updated": "交付物有更新",
    "decision_made": "形成明确决策",
    "issue_or_risk_found": "发现问题或风险",
    "follow_up_assigned": "形成后续跟进任务",
    "external_business_progress": "外部业务有实质进展",
    "substantive_approval": "完成有实质内容的审批",
}

INTERNAL_FEISHU_ID_RE = re.compile(r"\b(?:om|oc|ou)_[A-Za-z0-9_-]+\b")
MERGE_META_PREFIX = "<!-- worktrace:merge_meta "
UNKNOWN_METADATA_VALUE = "未明确"

@dataclass
class MarkdownEventStore(EventStore):
    config: RuntimeConfig
    last_warning_messages: list[str] = field(default_factory=list, init=False)
    last_partial_event_ids: list[str] = field(default_factory=list, init=False)

    def replace_day(
        self,
        target_date: str,
        events: list[WorkEvent],
        *,
        owner_display_name: str = "",
    ) -> StoreWriteResult:
        output_path = self.build_output_path(
            target_date,
            owner_display_name=owner_display_name,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = output_path.parent / f".{output_path.stem}.tmp.md"
        generated_at = now_iso(self.config.timezone)
        day_doc = DayDocument(
            date=target_date,
            events=list(events),
            generated_at=generated_at,
        )
        markdown = self.render_day_document(day_doc)
        try:
            temp_path.write_text(markdown, encoding="utf-8")
            parsed = self.parse_day_document(temp_path.read_text(encoding="utf-8"))
            if parsed.date != target_date or len(parsed.events) != len(events):
                raise StoreWriteError("Temporary markdown validation failed.")
            os.replace(temp_path, output_path)
        except OSError as exc:
            raise StoreWriteError(f"Failed to write markdown file: {output_path}") from exc

        return StoreWriteResult(
            output_path=str(output_path.resolve()),
            event_count=len(events),
            written_at=generated_at,
        )

    def read_day(self, target_date: str) -> DayDocument | None:
        output_path = self.build_output_path(target_date)
        if not output_path.exists():
            output_path = self._find_existing_day_path(target_date)
        if output_path is None:
            return None
        return self.parse_day_document(output_path.read_text(encoding="utf-8"))

    def build_output_path(self, target_date: str, *, owner_display_name: str = "") -> Path:
        year, month, _day = target_date.split("-")
        filename = build_personal_markdown_filename(target_date, owner_display_name)
        return self.config.data_root / year / month / filename

    def _find_existing_day_path(self, target_date: str) -> Path | None:
        year, month, _day = target_date.split("-")
        day_dir = self.config.data_root / year / month
        if not day_dir.exists():
            return None
        candidates: list[tuple[int, str, Path]] = []
        for path in sorted(day_dir.glob("*.md")):
            parsed = parse_worktrace_markdown_filename(path.name)
            if parsed.target_date != target_date or parsed.is_merged:
                continue
            score = 2
            if parsed.stem == target_date:
                score = 0
            elif parsed.stem.startswith(f"{target_date}-"):
                score = 1
            candidates.append((score, path.name, path))
        if not candidates:
            return None
        return sorted(candidates)[0][2]

    def render_day_document(self, day_doc: DayDocument) -> str:
        front_matter = "\n".join(
            [
                f"date: {day_doc.date}",
                f"event_count: {len(day_doc.events)}",
                f"generated_at: {day_doc.generated_at}",
                f"generator: {self.config.generator_name}",
                f"skill_version: {__version__}",
            ]
        )

        event_blocks = "\n\n".join(
            self._render_event(index, event)
            for index, event in enumerate(day_doc.events, start=1)
        )
        if not event_blocks:
            event_blocks = "_当天没有提炼出需要保留的工作事件。_"

        return (
            f"---\n{front_matter}\n---\n\n"
            f"# 工作事件日报 · {day_doc.date}\n\n"
            "## 事件列表\n\n"
            f"{event_blocks}\n\n"
            f"生成时间: {day_doc.generated_at}\n"
            "来源: 飞书沟通记录自动整理\n"
            "隐私声明: 仅含与本人直接相关的工作事件，不含原始聊天记录\n"
        )

    def _render_event(self, index: int, event: WorkEvent) -> str:
        link_lines = self._render_file_links(event.file_links)
        source_lines = self._render_source_lines(event)
        retention_reason_label = self._render_retention_reason(event.retention_reason)
        title = self._normalize_public_text(event.title)
        content = self._normalize_public_text(event.content)
        object_hint = self._normalize_public_text(event.object_hint)
        retention_detail = self._normalize_public_text(event.retention_detail)
        action_labels = self._render_action_labels(event.action_labels)
        relation_label = (
            "协作方式"
            if event.source_people or event.source_event_ids
            else "本人参与方式"
        )
        self_relations = self._render_self_relations(event.self_relations)
        merge_meta = self._render_merge_meta(event)
        manual_edit_line = self._render_manual_edit_line(event)
        event_id = event.event_id
        if INTERNAL_FEISHU_ID_RE.search(event_id):
            event_id = stable_event_id(event.date, [], event_id)
        return (
            f'<!-- worktrace:event:start event_id="{event_id}" -->\n'
            f"<!-- worktrace:retention_reason: {event.retention_reason} -->\n"
            f"{merge_meta}\n"
            f"### {index}. {title}\n\n"
            f"- **日期**: {event.date}\n"
            f"- **主要动作**: {action_labels}\n"
            f"- **内容**: {content}\n"
            f"- **具体对象**: {object_hint}\n"
            f"- **{relation_label}**: {self_relations}\n"
            f"- **保留理由**: {retention_reason_label}\n"
            f"- **保留依据**: {retention_detail}\n"
            f"{manual_edit_line}"
            f"{source_lines}"
            f"- **涉及文件**:\n{link_lines}\n"
            "<!-- worktrace:event:end -->"
        ).strip()

    def _render_metadata_text(self, value: str) -> str:
        cleaned = self._normalize_public_text(value.strip())
        return cleaned or UNKNOWN_METADATA_VALUE

    def _render_metadata_values(self, values: list[str]) -> str:
        cleaned = [
            self._normalize_public_text(value.strip())
            for value in values
            if value.strip()
        ]
        return "、".join(dict.fromkeys(cleaned)) or UNKNOWN_METADATA_VALUE

    def _render_self_relations(self, relations: list[str]) -> str:
        labels_by_key = {
            item.key: item.label for item in self.config.self_relation_types
        }
        labels = [labels_by_key.get(relation, relation) for relation in relations]
        return self._render_metadata_values(labels)

    def _render_action_labels(self, actions: list[str]) -> str:
        labels_by_key = {
            item.key: item.label for item in self.config.action_label_types
        }
        labels = [labels_by_key.get(action, action) for action in actions]
        return self._render_metadata_values(labels)

    def _render_merge_meta(self, event: WorkEvent) -> str:
        evidence_fingerprints = list(
            dict.fromkeys(
                [
                    *(
                        value
                        for value in event.evidence_fingerprints
                        if is_sha256_fingerprint(value)
                    ),
                    *(evidence_fingerprint(message_id) for message_id in event.source_message_ids),
                ]
            )
        )
        file_keys = list(
            dict.fromkeys(
                [
                    *(value for value in event.file_keys if is_sha256_fingerprint(value)),
                    *(
                        key
                        for link in event.file_links
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
        self_relations = [
            relation
            for relation in dict.fromkeys(event.self_relations)
            if not INTERNAL_FEISHU_ID_RE.search(relation)
        ]
        conversation_fingerprints = list(
            dict.fromkeys(
                value
                for value in event.conversation_fingerprints
                if is_sha256_fingerprint(value)
            )
        )
        allowed_manual_edit_types = {
            item.key for item in self.config.manual_edit_types
        }
        manual_edit_type = event.manual_edit_type.strip()
        if manual_edit_type not in allowed_manual_edit_types:
            manual_edit_type = ""
        source_manual_edit_types = list(
            dict.fromkeys(
                value.strip()
                for value in event.source_manual_edit_types
                if value.strip() in allowed_manual_edit_types
            )
        )
        content_fingerprint = (
            event.content_fingerprint
            if is_sha256_fingerprint(event.content_fingerprint)
            else self._event_content_fingerprint(event)
        )
        payload = {
            "version": 3,
            "self_relations": self_relations,
            "evidence_fingerprints": evidence_fingerprints,
            "conversation_fingerprints": conversation_fingerprints,
            "file_keys": file_keys,
            "manual_edit_type": manual_edit_type,
            "source_manual_edit_types": source_manual_edit_types,
            "content_fingerprint": content_fingerprint,
            "source_report_owners": list(
                dict.fromkeys(
                    owner.strip()
                    for owner in event.source_report_owners
                    if owner.strip() and not INTERNAL_FEISHU_ID_RE.search(owner)
                )
            ),
        }
        source_event_ids = list(
            dict.fromkeys(
                source_event_id.strip()
                for source_event_id in event.source_event_ids
                if source_event_id.strip()
                and not INTERNAL_FEISHU_ID_RE.search(source_event_id)
            )
        )
        if source_event_ids:
            payload["source_event_ids"] = source_event_ids
        return f"{MERGE_META_PREFIX}{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))} -->"

    def _render_manual_edit_line(self, event: WorkEvent) -> str:
        labels_by_key = {
            item.key: item.label for item in self.config.manual_edit_types
        }
        edit_types = list(
            dict.fromkeys(
                [
                    event.manual_edit_type,
                    *event.source_manual_edit_types,
                ]
            )
        )
        labels = [labels_by_key[key] for key in edit_types if key in labels_by_key]
        if not labels:
            return ""
        return (
            f"- **{self.config.manual_edit_field_label}**: "
            f"{'、'.join(labels)}\n"
        )

    def _event_content_fingerprint(self, event: WorkEvent) -> str:
        files: list[dict[str, str]] = []
        for link in event.file_links:
            safe_url = self._redact_sensitive_query_params(link.url)
            label = self._normalize_public_text(link.title.strip() or safe_url)
            files.append({"title": label, "url": safe_url})
        return event_content_fingerprint(
            {
                "date": event.date.strip(),
                "title": self._normalize_public_text(event.title),
                "action_labels": self._render_action_labels(event.action_labels),
                "content": self._normalize_public_text(event.content),
                "object_hint": self._normalize_public_text(event.object_hint),
                "self_relations": self._render_self_relations(event.self_relations),
                "retention_reason": self._render_retention_reason(
                    event.retention_reason
                ),
                "retention_detail": self._normalize_public_text(
                    event.retention_detail
                ),
                "source_people": [
                    self._normalize_public_text(value)
                    for value in event.source_people
                    if value.strip()
                ],
                "source_report_owners": [
                    self._normalize_public_text(value)
                    for value in event.source_report_owners
                    if value.strip()
                ],
                "files": files,
            }
        )

    def _redact_internal_ids(self, value: str) -> str:
        return INTERNAL_FEISHU_ID_RE.sub("[内部消息ID已隐藏]", value)

    def _normalize_public_text(self, value: str) -> str:
        return normalize_sentence_final_ma(self._redact_internal_ids(value))

    def _render_retention_reason(self, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            return ""
        return RETENTION_REASON_LABELS.get(stripped, stripped)

    def _render_file_links(self, file_links: list[EventFileLink]) -> str:
        if not file_links:
            return "  - 无"

        lines: list[str] = []
        for link in file_links:
            safe_url = self._redact_sensitive_query_params(link.url)
            label = link.title.strip() or safe_url
            if not safe_url:
                lines.append(f"  - {self._quote_plain_file_name(label)}")
                continue
            if label == link.url:
                label = safe_url
            lines.append(f"  - [{label}]({safe_url})")
        return "\n".join(lines)

    def _quote_plain_file_name(self, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            return "无"
        if stripped.startswith("《") and stripped.endswith("》"):
            return stripped
        return f"《{stripped}》"

    def _redact_sensitive_query_params(self, value: str) -> str:
        try:
            parts = urlsplit(value)
        except ValueError:
            return value
        if not parts.query:
            return value

        changed = False
        query_pairs: list[tuple[str, str]] = []
        for key, raw_value in parse_qsl(parts.query, keep_blank_values=True):
            if key.lower() in SENSITIVE_QUERY_KEYS:
                query_pairs.append((key, "REDACTED"))
                changed = True
            else:
                query_pairs.append((key, raw_value))
        if not changed:
            return value

        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode(query_pairs, doseq=True),
                parts.fragment,
            )
        )

    def parse_day_document(
        self,
        markdown_text: str,
        *,
        allow_trailing_partial: bool = False,
    ) -> DayDocument:
        self.last_warning_messages = []
        self.last_partial_event_ids = []
        if not markdown_text.startswith("---\n"):
            raise StoreWriteError("Markdown front matter is missing.")
        _, front_matter, body = markdown_text.split("---\n", 2)
        meta = self._parse_front_matter(front_matter)
        date = str(meta["date"])
        generated_at = str(meta["generated_at"])
        events = self._parse_events(
            body,
            document_date=date,
            allow_trailing_partial=allow_trailing_partial,
        )
        return DayDocument(
            date=date,
            events=events,
            generated_at=generated_at,
        )

    def _parse_events(
        self,
        body: str,
        *,
        document_date: str,
        allow_trailing_partial: bool = False,
    ) -> list[WorkEvent]:
        positioned_events: list[tuple[int, WorkEvent]] = []
        occupied_ranges: list[tuple[int, int]] = []
        seen_event_ids: set[str] = set()
        cursor = 0
        start_marker = '<!-- worktrace:event:start event_id="'
        end_marker = "<!-- worktrace:event:end -->"

        while True:
            start = body.find(start_marker, cursor)
            if start == -1:
                break
            event_id_start = start + len(start_marker)
            event_id_end = body.find('" -->', event_id_start)
            if event_id_end == -1:
                if (
                    allow_trailing_partial
                    and positioned_events
                    and body.find(start_marker, event_id_start) == -1
                ):
                    self.last_partial_event_ids.append("unknown")
                    self.last_warning_messages.append(
                        "Skipped malformed trailing event block: unknown."
                    )
                    break
                raise StoreWriteError("Malformed event block: missing event_id terminator.")
            event_id = body[event_id_start:event_id_end]
            try:
                block_end, next_cursor = self._locate_event_block_end(
                    body=body,
                    event_id=event_id,
                    search_start=event_id_end,
                    start_marker=start_marker,
                    end_marker=end_marker,
                )
            except StoreWriteError:
                if not allow_trailing_partial or not positioned_events:
                    raise
                self.last_partial_event_ids.append(event_id)
                self.last_warning_messages.append(
                    f"Skipped malformed trailing event block: {event_id}."
                )
                break
            block = body[event_id_end + 5:block_end].strip()
            forced_manual_edit_type = ""
            if event_id in seen_event_ids:
                event_id = self._manual_event_id(
                    document_date,
                    block,
                    occurrence=len(positioned_events) + 1,
                )
                forced_manual_edit_type = "manual_added"
            event = self._parse_event_block(
                block,
                event_id=event_id,
                forced_manual_edit_type=forced_manual_edit_type,
            )
            positioned_events.append((start, event))
            occupied_ranges.append((start, next_cursor))
            seen_event_ids.add(event.event_id)
            cursor = next_cursor

        heading_matches = list(
            re.finditer(r"(?m)^###[ \t]+\d+\.[ \t]*(.+)$", body)
        )
        footer_start = self._find_day_footer_start(body, 0)
        for index, match in enumerate(heading_matches):
            start = match.start()
            if any(range_start <= start < range_end for range_start, range_end in occupied_ranges):
                continue
            end = (
                heading_matches[index + 1].start()
                if index + 1 < len(heading_matches)
                else len(body)
            )
            if footer_start != -1 and start < footer_start < end:
                end = footer_start
            block = body[start:end].strip()
            if not self._looks_like_markerless_event(block):
                continue
            event_id = self._manual_event_id(
                document_date,
                block,
                occurrence=index + 1,
            )
            while event_id in seen_event_ids:
                event_id = stable_event_id(
                    document_date,
                    [],
                    f"manual-added:{event_id}:{len(seen_event_ids)}",
                )
            event = self._parse_event_block(
                block,
                event_id=event_id,
                forced_manual_edit_type="manual_added",
            )
            positioned_events.append((start, event))
            seen_event_ids.add(event.event_id)

        return [event for _, event in sorted(positioned_events, key=lambda item: item[0])]

    def _parse_event_block(
        self,
        block: str,
        *,
        event_id: str,
        forced_manual_edit_type: str = "",
    ) -> WorkEvent:
        title = self._extract_value(
            block,
            "- **事件标题**: ",
            "- 事件标题: ",
        ) or self._extract_event_heading_title(block)
        content = self._extract_value(
            block,
            "- **内容**: ",
            "- 事件内容: ",
        )
        object_hint = self._extract_value(
            block,
            "- **具体对象**: ",
            "- 具体对象: ",
        )
        retention_reason = self._extract_retention_reason(block)
        retention_detail = self._extract_value(
            block,
            "- **保留依据**: ",
            "- 保留依据: ",
        )
        event_date = self._extract_value(
            block,
            "- **日期**: ",
            "- 日期: ",
        )
        source_people = self._parse_source_values(
            self._extract_value(block, "- 来源人员: ")
        )
        source_event_ids = self._parse_source_values(
            self._extract_value(block, "- 来源事件 ID: ")
        )
        source_report_owners = self._parse_source_values(
            self._extract_value(block, "- 来源负责人: ")
        )
        action_labels = self._parse_metadata_values(
            self._extract_value(block, "- **主要动作**: ")
        )
        visible_relations = self._parse_metadata_values(
            self._extract_value(
                block,
                "- **本人参与方式**: ",
                "- **协作方式**: ",
            )
        )
        merge_meta = self._extract_merge_meta(block, event_id=event_id)
        if forced_manual_edit_type == "manual_added":
            merge_meta = {}
            source_event_ids = []
            source_report_owners = []
        elif not source_event_ids:
            source_event_ids = list(merge_meta.get("source_event_ids", []))
        if not source_report_owners:
            source_report_owners = list(
                merge_meta.get("source_report_owners", [])
            )
        self_relations = list(merge_meta.get("self_relations", []))
        if not self_relations:
            self_relations = self._relation_keys_from_labels(visible_relations)
        manual_edit_type = forced_manual_edit_type or str(
            merge_meta.get("manual_edit_type", "")
        )
        source_manual_edit_types = list(
            merge_meta.get("source_manual_edit_types", [])
        )
        event = WorkEvent(
            date=event_date,
            event_id=event_id,
            title=title,
            content=content,
            file_links=self._extract_file_links(block),
            source_people=source_people,
            source_event_ids=source_event_ids,
            source_report_owners=source_report_owners,
            object_hint=object_hint,
            retention_reason=retention_reason,
            retention_detail=retention_detail,
            action_labels=action_labels,
            self_relations=self_relations,
            evidence_fingerprints=list(
                merge_meta.get("evidence_fingerprints", [])
            ),
            conversation_fingerprints=list(
                merge_meta.get("conversation_fingerprints", [])
            ),
            file_keys=list(merge_meta.get("file_keys", [])),
            manual_edit_type=manual_edit_type,
            source_manual_edit_types=source_manual_edit_types,
            content_fingerprint=str(merge_meta.get("content_fingerprint", "")),
        )
        actual_fingerprint = self._event_content_fingerprint(event)
        merge_meta_version = int(merge_meta.get("version", 0))
        if not event.manual_edit_type:
            if (
                merge_meta_version == 3
                and event.content_fingerprint
                and event.content_fingerprint != actual_fingerprint
            ):
                event = WorkEvent.from_dict(
                    {**event.to_dict(), "manual_edit_type": "manual_modified"}
                )
            elif merge_meta_version == 0 or (
                merge_meta_version in {2, 3}
                and not event.conversation_fingerprints
                and not event.source_manual_edit_types
            ):
                event = WorkEvent.from_dict(
                    {**event.to_dict(), "manual_edit_type": "manual_unknown"}
                )
        return event

    def _looks_like_markerless_event(self, block: str) -> bool:
        return all(
            [
                self._extract_event_heading_title(block),
                self._extract_value(block, "- **日期**: ", "- 日期: "),
                self._extract_value(block, "- **内容**: ", "- 事件内容: "),
                self._extract_value(block, "- **具体对象**: ", "- 具体对象: "),
                self._extract_value(block, "- **保留理由**: ", "- 保留理由: "),
                self._extract_value(block, "- **保留依据**: ", "- 保留依据: "),
                (
                    "- **涉及文件**:" in block
                    or "- 涉及文件链接:" in block
                ),
            ]
        )

    def _manual_event_id(
        self,
        document_date: str,
        block: str,
        *,
        occurrence: int,
    ) -> str:
        title = self._extract_event_heading_title(block)
        content = self._extract_value(block, "- **内容**: ", "- 事件内容: ")
        object_hint = self._extract_value(
            block,
            "- **具体对象**: ",
            "- 具体对象: ",
        )
        return stable_event_id(
            document_date,
            [],
            f"manual-added:{title}|{content}|{object_hint}|{occurrence}",
        )

    def _locate_event_block_end(
        self,
        *,
        body: str,
        event_id: str,
        search_start: int,
        start_marker: str,
        end_marker: str,
    ) -> tuple[int, int]:
        explicit_end = body.find(end_marker, search_start)
        next_start = body.find(start_marker, search_start)
        footer_start = self._find_day_footer_start(body, search_start)

        if explicit_end != -1 and (next_start == -1 or explicit_end < next_start):
            return explicit_end, explicit_end + len(end_marker)

        implicit_end_candidates = [
            position
            for position in (next_start, footer_start)
            if position != -1
        ]
        if implicit_end_candidates:
            implicit_end = min(implicit_end_candidates)
            return implicit_end, implicit_end

        if explicit_end != -1:
            return explicit_end, explicit_end + len(end_marker)

        raise StoreWriteError(
            f"Malformed event block: missing end marker for event_id '{event_id}'."
        )

    def _find_day_footer_start(self, body: str, search_start: int) -> int:
        footer_markers = (
            "\n生成时间:",
            "\n来源:",
            "\n隐私声明:",
        )
        positions = [
            body.find(marker, search_start)
            for marker in footer_markers
        ]
        valid_positions = [position for position in positions if position != -1]
        if not valid_positions:
            return -1
        return min(valid_positions)

    def _extract_value(self, block: str, *prefixes: str) -> str:
        for line in block.splitlines():
            for prefix in prefixes:
                if line.startswith(prefix):
                    return line[len(prefix) :].strip()
        return ""

    def _extract_event_heading_title(self, block: str) -> str:
        for line in block.splitlines():
            match = re.fullmatch(r"###[ \t]+\d+\.[ \t]*(.*)", line)
            if match:
                return match.group(1).strip()
        return ""

    def _extract_retention_reason(self, block: str) -> str:
        marker = "<!-- worktrace:retention_reason: "
        for line in block.splitlines():
            if line.startswith(marker) and line.endswith(" -->"):
                return line[len(marker) : -len(" -->")].strip()
        visible_value = self._extract_value(
            block,
            "- 保留理由: ",
            "- **保留理由**: ",
        )
        keys_by_label = {
            label: key for key, label in RETENTION_REASON_LABELS.items()
        }
        return keys_by_label.get(visible_value, visible_value)

    def _render_source_values(self, values: list[str]) -> str:
        cleaned = [
            self._normalize_public_text(value.strip())
            for value in values
            if value.strip()
        ]
        if not cleaned:
            return "无"
        return "、".join(cleaned)

    def _render_source_lines(self, event: WorkEvent) -> str:
        if not event.source_people and not event.source_report_owners:
            return ""
        lines = [f"- 来源人员: {self._render_source_values(event.source_people)}"]
        if event.source_report_owners:
            lines.append(
                "- 来源负责人: "
                f"{self._render_source_values(event.source_report_owners)}"
            )
        return "\n".join(lines) + "\n"

    def _parse_source_values(self, value: str) -> list[str]:
        stripped = value.strip()
        if not stripped or stripped == "无":
            return []
        return [item.strip() for item in stripped.split("、") if item.strip()]

    def _parse_optional_metadata_value(self, value: str) -> str:
        stripped = value.strip()
        if stripped == UNKNOWN_METADATA_VALUE:
            return ""
        return stripped

    def _parse_metadata_values(self, value: str) -> list[str]:
        stripped = value.strip()
        if not stripped or stripped == UNKNOWN_METADATA_VALUE:
            return []
        return [item.strip() for item in stripped.split("、") if item.strip()]

    def _relation_keys_from_labels(self, labels: list[str]) -> list[str]:
        keys_by_label = {
            item.label: item.key for item in self.config.self_relation_types
        }
        return list(
            dict.fromkeys(keys_by_label.get(label, label) for label in labels)
        )

    def _extract_merge_meta(self, block: str, *, event_id: str) -> dict[str, object]:
        for line in block.splitlines():
            if not line.startswith(MERGE_META_PREFIX):
                continue
            if not line.endswith(" -->"):
                self._record_merge_meta_warning(event_id)
                return {}
            raw_payload = line[len(MERGE_META_PREFIX) : -len(" -->")]
            try:
                payload = json.loads(raw_payload)
            except json.JSONDecodeError:
                self._record_merge_meta_warning(event_id)
                return {}
            if not isinstance(payload, dict) or payload.get("version") not in {1, 2, 3}:
                self._record_merge_meta_warning(event_id)
                return {}
            version = int(payload["version"])
            parsed: dict[str, object] = {"version": version}
            keys = [
                "self_relations",
                "evidence_fingerprints",
                "file_keys",
                "source_report_owners",
            ]
            if version >= 2:
                keys.extend(["conversation_fingerprints", "source_event_ids"])
            if version >= 3:
                keys.append("source_manual_edit_types")
            for key in keys:
                value = payload.get(key, [])
                if not isinstance(value, list) or not all(
                    isinstance(item, str) for item in value
                ):
                    self._record_merge_meta_warning(event_id)
                    return {}
                if key in {
                    "self_relations",
                    "source_event_ids",
                    "source_report_owners",
                    "source_manual_edit_types",
                } and any(
                    INTERNAL_FEISHU_ID_RE.search(item) for item in value
                ):
                    self._record_merge_meta_warning(event_id)
                    return {}
                if key not in {
                    "self_relations",
                    "source_event_ids",
                    "source_report_owners",
                    "source_manual_edit_types",
                }:
                    if not all(is_sha256_fingerprint(item) for item in value):
                        self._record_merge_meta_warning(event_id)
                        return {}
                parsed[key] = list(dict.fromkeys(value))
            if version >= 3:
                allowed_manual_edit_types = {
                    item.key for item in self.config.manual_edit_types
                }
                manual_edit_type = payload.get("manual_edit_type", "")
                content_fingerprint = payload.get("content_fingerprint", "")
                if (
                    not isinstance(manual_edit_type, str)
                    or (
                        manual_edit_type
                        and manual_edit_type not in allowed_manual_edit_types
                    )
                    or not isinstance(content_fingerprint, str)
                    or not is_sha256_fingerprint(content_fingerprint)
                    or any(
                        value not in allowed_manual_edit_types
                        for value in parsed.get("source_manual_edit_types", [])
                    )
                ):
                    self._record_merge_meta_warning(event_id)
                    return {}
                parsed["manual_edit_type"] = manual_edit_type
                parsed["content_fingerprint"] = content_fingerprint
            return parsed
        return {}

    def _record_merge_meta_warning(self, event_id: str) -> None:
        self.last_warning_messages.append(
            f"Ignored damaged merge metadata for event {event_id}."
        )

    def _extract_file_links(self, block: str) -> list[EventFileLink]:
        lines = block.splitlines()
        collected: list[EventFileLink] = []
        in_section = False
        for line in lines:
            if line.startswith("- 涉及文件链接:") or line.startswith("- **涉及文件**:"):
                in_section = True
                continue
            if not in_section:
                continue
            stripped = line.strip()
            if not stripped.startswith("- "):
                continue
            raw_value = stripped[2:].strip()
            if raw_value == "无":
                return []
            if raw_value.startswith("[") and "](" in raw_value and raw_value.endswith(")"):
                label_end = raw_value.find("](")
                title = raw_value[1:label_end]
                url = raw_value[label_end + 2 : -1]
                collected.append(
                    EventFileLink(
                        url=url,
                        title=title,
                        link_type="normal",
                    )
                )
                continue
            title = raw_value
            if title.startswith("《") and title.endswith("》"):
                title = title[1:-1].strip()
            if title:
                collected.append(
                    EventFileLink(
                        url="",
                        title=title,
                        link_type="attachment",
                    )
                )
        return collected

    def _parse_front_matter(self, text: str) -> dict[str, str]:
        parsed: dict[str, str] = {}
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or ":" not in stripped:
                continue
            key, value = stripped.split(":", 1)
            parsed[key.strip()] = value.strip()
        return parsed
