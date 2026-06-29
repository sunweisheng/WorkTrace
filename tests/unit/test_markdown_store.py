from __future__ import annotations

from pathlib import Path

from src.worktrace.config import RuntimeConfig
from src.worktrace.models import EventFileLink, WorkEvent
from src.worktrace.stores.markdown import MarkdownEventStore


def test_markdown_store_roundtrip(tmp_path: Path) -> None:
    store = MarkdownEventStore(config=RuntimeConfig(data_root=tmp_path / "data"))
    write_result = store.replace_day(
        "2026-06-22",
        [
            WorkEvent(
                date="2026-06-22",
                event_id="evt1",
                title="主题",
                content="内容",
                source_message_ids=["om_1"],
                file_links=[],
            )
        ],
    )
    loaded = store.read_day("2026-06-22")

    assert write_result.event_count == 1
    assert loaded is not None
    assert loaded.events[0].event_id == "evt1"


def test_markdown_store_renders_public_event_fields(tmp_path: Path) -> None:
    store = MarkdownEventStore(config=RuntimeConfig(data_root=tmp_path / "data"))
    write_result = store.replace_day(
        "2026-06-22",
        [
            WorkEvent(
                date="2026-06-22",
                event_id="evt1",
                title="主题一",
                content="内容一",
                source_message_ids=["om_1"],
                file_links=[
                    EventFileLink(
                        url="https://foo.feishu.cn/docx/abc",
                        title="方案一",
                        link_type="feishu_doc",
                    )
                ],
            ),
            WorkEvent(
                date="2026-06-22",
                event_id="evt2",
                title="主题二",
                content="内容二",
                source_message_ids=["om_2"],
                file_links=[],
            ),
        ],
    )

    content = Path(write_result.output_path).read_text(encoding="utf-8")

    assert "## 每日工作事件" in content
    assert "### 主题一" in content
    assert "- 日期: 2026-06-22" in content
    assert "- 事件标题: 主题一" in content
    assert "- 事件内容: 内容一" in content
    assert "- 来源人员:" not in content
    assert "- 来源事件 ID:" not in content
    assert "- 涉及文件链接:" in content
    assert "[方案一](https://foo.feishu.cn/docx/abc)" in content
    assert "### 主题二" in content
    assert "- 事件标题: 主题二" in content
    assert "- 事件内容: 内容二" in content
    assert "  - 无" in content


def test_markdown_store_uses_owner_display_name_in_filename(tmp_path: Path) -> None:
    store = MarkdownEventStore(config=RuntimeConfig(data_root=tmp_path / "data"))

    write_result = store.replace_day(
        "2026-06-22",
        [],
        owner_display_name="孙 伟/盛",
    )

    assert Path(write_result.output_path).name == "2026-06-22-孙_伟_盛.md"


def test_markdown_store_reads_owner_named_day_by_date(tmp_path: Path) -> None:
    store = MarkdownEventStore(config=RuntimeConfig(data_root=tmp_path / "data"))
    store.replace_day(
        "2026-06-22",
        [
            WorkEvent(
                date="2026-06-22",
                event_id="evt1",
                title="主题",
                content="内容",
                source_message_ids=["om_1"],
                file_links=[],
            )
        ],
        owner_display_name="孙伟盛",
    )

    loaded = store.read_day("2026-06-22")

    assert loaded is not None
    assert loaded.events[0].event_id == "evt1"


def test_markdown_store_roundtrip_keeps_file_links(tmp_path: Path) -> None:
    store = MarkdownEventStore(config=RuntimeConfig(data_root=tmp_path / "data"))
    store.replace_day(
        "2026-06-22",
        [
            WorkEvent(
                date="2026-06-22",
                event_id="evt1",
                title="主题",
                content="内容",
                source_message_ids=["om_1"],
                file_links=[
                    EventFileLink(
                        url="https://foo.feishu.cn/docx/abc",
                        title="方案",
                        link_type="feishu_doc",
                    )
                ],
            )
        ],
    )

    loaded = store.read_day("2026-06-22")

    assert loaded is not None
    assert len(loaded.events) == 1
    assert loaded.events[0].event_id == "evt1"
    assert loaded.events[0].title == "主题"
    assert loaded.events[0].source_message_ids == []
    assert loaded.events[0].source_people == []
    assert loaded.events[0].source_event_ids == []
    assert loaded.events[0].file_links[0].url == "https://foo.feishu.cn/docx/abc"


def test_markdown_store_roundtrip_keeps_collected_source_fields(tmp_path: Path) -> None:
    store = MarkdownEventStore(config=RuntimeConfig(data_root=tmp_path / "data"))
    store.replace_day(
        "2026-06-22",
        [
            WorkEvent(
                date="2026-06-22",
                event_id="evt1",
                title="主题",
                content="内容",
                source_people=["张三", "李四"],
                source_event_ids=["evt-a", "evt-b"],
                object_hint="客户合同",
                retention_reason="substantive_approval",
                retention_detail="反馈客户合同付款条款问题。",
                file_links=[],
            )
        ],
    )

    loaded = store.read_day("2026-06-22")

    assert loaded is not None
    assert loaded.events[0].source_people == ["张三", "李四"]
    assert loaded.events[0].source_event_ids == ["evt-a", "evt-b"]
    assert loaded.events[0].object_hint == "客户合同"
    assert loaded.events[0].retention_reason == "substantive_approval"
    assert loaded.events[0].retention_detail == "反馈客户合同付款条款问题。"


def test_markdown_store_preserves_event_order(tmp_path: Path) -> None:
    store = MarkdownEventStore(config=RuntimeConfig(data_root=tmp_path / "data"))
    store.replace_day(
        "2026-06-22",
        [
            WorkEvent(
                date="2026-06-22",
                event_id="evt_b",
                title="后发生",
                content="内容二",
                source_message_ids=["om_2"],
                file_links=[],
            ),
            WorkEvent(
                date="2026-06-22",
                event_id="evt_a",
                title="先发生",
                content="内容一",
                source_message_ids=["om_1"],
                file_links=[],
            ),
        ],
    )

    loaded = store.read_day("2026-06-22")

    assert loaded is not None
    assert [event.title for event in loaded.events] == ["后发生", "先发生"]
