from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..config import RuntimeConfig
from ..errors import StoreWriteError
from ..models import DayDocument, EventFileLink, StoreWriteResult, WorkEvent
from ..utils.dates import now_iso
from .base import EventStore


@dataclass
class MarkdownEventStore(EventStore):
    config: RuntimeConfig

    def replace_day(
        self,
        target_date: str,
        events: list[WorkEvent],
    ) -> StoreWriteResult:
        output_path = self.build_output_path(target_date)
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
            return None
        return self.parse_day_document(output_path.read_text(encoding="utf-8"))

    def build_output_path(self, target_date: str) -> Path:
        year, month, _day = target_date.split("-")
        return self.config.data_root / year / month / f"{target_date}.md"

    def render_day_document(self, day_doc: DayDocument) -> str:
        front_matter = "\n".join(
            [
                f"date: {day_doc.date}",
                f"event_count: {len(day_doc.events)}",
                f"generated_at: {day_doc.generated_at}",
                f"generator: {self.config.generator_name}",
            ]
        )

        event_blocks = "\n\n".join(self._render_event(event) for event in day_doc.events)
        if not event_blocks:
            event_blocks = "_当天没有提炼出需要保留的工作事件。_"

        return (
            f"---\n{front_matter}\n---\n\n"
            f"# WorkTrace {day_doc.date}\n\n"
            "## 每日工作事件\n\n"
            f"{event_blocks}\n"
        )

    def _render_event(self, event: WorkEvent) -> str:
        link_lines = self._render_file_links(event.file_links)
        source_lines = self._render_source_lines(event)
        return (
            f'<!-- worktrace:event:start event_id="{event.event_id}" -->\n'
            f"### {event.title}\n\n"
            f"- 日期: {event.date}\n"
            f"- 事件标题: {event.title}\n"
            f"- 事件内容: {event.content}\n"
            f"- 具体对象: {event.object_hint}\n"
            f"- 保留理由: {event.retention_reason}\n"
            f"- 保留依据: {event.retention_detail}\n"
            f"{source_lines}"
            f"- 涉及文件链接:\n{link_lines}\n"
            "<!-- worktrace:event:end -->"
        ).strip()

    def _render_file_links(self, file_links: list[EventFileLink]) -> str:
        if not file_links:
            return "  - 无"

        lines: list[str] = []
        for link in file_links:
            label = link.title.strip() or link.url
            lines.append(f"  - [{label}]({link.url})")
        return "\n".join(lines)

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
            event_id = body[event_id_start:event_id_end]
            block_end = body.find(end_marker, event_id_end)
            block = body[event_id_end + 5:block_end].strip()
            title = self._extract_value(block, "- 事件标题: ")
            content = self._extract_value(block, "- 事件内容: ")
            object_hint = self._extract_value(block, "- 具体对象: ")
            retention_reason = self._extract_value(block, "- 保留理由: ")
            retention_detail = self._extract_value(block, "- 保留依据: ")
            event_date = self._extract_value(block, "- 日期: ")
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
            cursor = block_end + len(end_marker)

        return events

    def _extract_value(self, block: str, prefix: str) -> str:
        for line in block.splitlines():
            if line.startswith(prefix):
                return line[len(prefix) :].strip()
        return ""

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
            if line.startswith("- 涉及文件链接:"):
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
