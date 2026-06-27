from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..config import RuntimeConfig
from ..errors import StoreWriteError
from ..models import DayDocument, StoreWriteResult, WorkEvent
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
            events=sorted(events, key=lambda item: item.event_id),
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

        my_daily_report = self._render_my_daily_report(day_doc.events)
        event_blocks = "\n\n".join(self._render_event(event) for event in day_doc.events)
        if not event_blocks:
            event_blocks = ""

        return (
            f"---\n{front_matter}\n---\n\n"
            f"# WorkTrace {day_doc.date}\n\n"
            "## 我的日报\n\n"
            f"{my_daily_report}\n\n"
            "## 事项列表\n\n"
            f"{event_blocks}\n"
        )

    def _render_my_daily_report(self, events: list[WorkEvent]) -> str:
        if not events:
            return ""

        return "\n\n".join(self._render_my_daily_report_item(event) for event in events)

    def _render_my_daily_report_item(self, event: WorkEvent) -> str:
        return (
            f"### {event.topic}\n\n"
            f"- 日期: {event.date}\n"
            f"- 事件: {event.topic}\n"
            f"- 事件内容: {event.content}"
        ).strip()

    def _render_event(self, event: WorkEvent) -> str:
        return (
            f'<!-- worktrace:event:start event_id="{event.event_id}" -->\n'
            f"### {event.event_id} {event.topic}\n\n"
            f"- date: {event.date}\n"
            f"- event_id: {event.event_id}\n"
            f"- topic: {event.topic}\n\n"
            "#### content\n\n"
            f"{event.content}\n"
            "<!-- worktrace:event:end -->"
        ).strip()

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
            lines = block.splitlines()
            title_line = lines[0].strip("# ").strip()
            topic = title_line[len(event_id) :].strip() if title_line.startswith(event_id) else title_line
            content = self._extract_section(block, "#### content", None)
            date_line = next((line for line in lines if line.startswith("- date: ")), "")
            event_date = date_line.replace("- date: ", "").strip()
            events.append(
                WorkEvent(
                    date=event_date,
                    event_id=event_id,
                    topic=topic,
                    content=content,
                )
            )
            cursor = block_end + len(end_marker)

        return sorted(events, key=lambda item: item.event_id)

    def _extract_section(self, block: str, section: str, next_section: str | None) -> str:
        start = block.index(section) + len(section)
        if next_section is None:
            return block[start:].strip()
        end = block.index(next_section, start)
        return block[start:end].strip()

    def _parse_front_matter(self, text: str) -> dict[str, str]:
        parsed: dict[str, str] = {}
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or ":" not in stripped:
                continue
            key, value = stripped.split(":", 1)
            parsed[key.strip()] = value.strip()
        return parsed
