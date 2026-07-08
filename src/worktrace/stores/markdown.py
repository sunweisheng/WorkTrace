from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..config import RuntimeConfig
from ..errors import StoreWriteError
from ..models import DayDocument, EventFileLink, StoreWriteResult, WorkEvent
from ..utils.dates import now_iso
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


@dataclass
class MarkdownEventStore(EventStore):
    config: RuntimeConfig

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
        return (
            f'<!-- worktrace:event:start event_id="{event.event_id}" -->\n'
            f"<!-- worktrace:retention_reason: {event.retention_reason} -->\n"
            f"### {index}. {title}\n\n"
            f"- **日期**: {event.date}\n"
            f"- **事件标题**: {title}\n"
            f"- **内容**: {content}\n"
            f"- **具体对象**: {object_hint}\n"
            f"- **保留理由**: {retention_reason_label}\n"
            f"- **保留依据**: {retention_detail}\n"
            f"{source_lines}"
            f"- **涉及文件**:\n{link_lines}\n"
            "<!-- worktrace:event:end -->"
        ).strip()

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
            if label == link.url:
                label = safe_url
            lines.append(f"  - [{label}]({safe_url})")
        return "\n".join(lines)

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

    def parse_day_document(self, markdown_text: str) -> DayDocument:
        if not markdown_text.startswith("---\n"):
            raise StoreWriteError("Markdown front matter is missing.")
        _, front_matter, body = markdown_text.split("---\n", 2)
        meta = self._parse_front_matter(front_matter)
        date = str(meta["date"])
        generated_at = str(meta["generated_at"])
        events = self._parse_events(body)
        return DayDocument(
            date=date,
            events=events,
            generated_at=generated_at,
        )

    def _parse_events(self, body: str) -> list[WorkEvent]:
        events: list[WorkEvent] = []
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
                raise StoreWriteError("Malformed event block: missing event_id terminator.")
            event_id = body[event_id_start:event_id_end]
            block_end, next_cursor = self._locate_event_block_end(
                body=body,
                event_id=event_id,
                search_start=event_id_end,
                start_marker=start_marker,
                end_marker=end_marker,
            )
            block = body[event_id_end + 5:block_end].strip()
            title = self._extract_value(
                block,
                "- **事件标题**: ",
                "- 事件标题: ",
            )
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
            file_links = self._extract_file_links(block)
            events.append(
                WorkEvent(
                    date=event_date,
                    event_id=event_id,
                    title=title,
                    content=content,
                    file_links=file_links,
                    source_people=source_people,
                    source_event_ids=source_event_ids,
                    object_hint=object_hint,
                    retention_reason=retention_reason,
                    retention_detail=retention_detail,
                )
            )
            cursor = next_cursor

        return events

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

    def _extract_retention_reason(self, block: str) -> str:
        marker = "<!-- worktrace:retention_reason: "
        for line in block.splitlines():
            if line.startswith(marker) and line.endswith(" -->"):
                return line[len(marker) : -len(" -->")].strip()
        return self._extract_value(block, "- 保留理由: ", "- **保留理由**: ")

    def _render_source_values(self, values: list[str]) -> str:
        cleaned = [value.strip() for value in values if value.strip()]
        if not cleaned:
            return "无"
        return "、".join(cleaned)

    def _render_source_lines(self, event: WorkEvent) -> str:
        if not event.source_people and not event.source_event_ids:
            return ""
        return (
            f"- 来源人员: {self._render_source_values(event.source_people)}\n"
            f"- 来源事件 ID: {self._render_source_values(event.source_event_ids)}\n"
        )

    def _parse_source_values(self, value: str) -> list[str]:
        stripped = value.strip()
        if not stripped or stripped == "无":
            return []
        return [item.strip() for item in stripped.split("、") if item.strip()]

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
