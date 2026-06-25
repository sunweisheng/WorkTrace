from __future__ import annotations

from pathlib import Path

from src.worktrace.config import RuntimeConfig
from src.worktrace.models import WorkEvent
from src.worktrace.stores.markdown import MarkdownEventStore


def test_markdown_store_roundtrip(tmp_path: Path) -> None:
    store = MarkdownEventStore(config=RuntimeConfig(data_root=tmp_path / "data"))
    write_result = store.replace_day(
        "2026-06-22",
        [
            WorkEvent(
                date="2026-06-22",
                event_id="evt1",
                topic="主题",
                content="内容",
                result="结果",
            )
        ],
    )
    loaded = store.read_day("2026-06-22")

    assert write_result.validation_passed is True
    assert loaded is not None
    assert loaded.events[0].event_id == "evt1"


def test_markdown_store_renders_my_daily_report_section(tmp_path: Path) -> None:
    store = MarkdownEventStore(config=RuntimeConfig(data_root=tmp_path / "data"))
    write_result = store.replace_day(
        "2026-06-22",
        [
            WorkEvent(
                date="2026-06-22",
                event_id="evt1",
                topic="主题一",
                content="内容一",
                result="结果一",
            ),
            WorkEvent(
                date="2026-06-22",
                event_id="evt2",
                topic="主题二",
                content="内容二",
                result="",
            ),
        ],
    )

    content = Path(write_result.output_path).read_text(encoding="utf-8")

    assert "## 我的日报" in content
    assert "### 主题一" in content
    assert "- 日期: 2026-06-22" in content
    assert "- 事件: 主题一" in content
    assert "- 事件内容: 内容一" in content
    assert "- 结果: 结果一" in content
    assert "### 主题二" in content
    assert "- 事件: 主题二" in content
    assert "- 事件内容: 内容二" in content
    assert "- 结果: " in content
    assert "## 事项列表" in content


def test_markdown_store_my_daily_report_does_not_break_roundtrip(tmp_path: Path) -> None:
    store = MarkdownEventStore(config=RuntimeConfig(data_root=tmp_path / "data"))
    store.replace_day(
        "2026-06-22",
        [
            WorkEvent(
                date="2026-06-22",
                event_id="evt1",
                topic="主题",
                content="内容",
                result="",
            )
        ],
    )

    loaded = store.read_day("2026-06-22")

    assert loaded is not None
    assert len(loaded.events) == 1
    assert loaded.events[0].event_id == "evt1"
    assert loaded.events[0].result == ""
