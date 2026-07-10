from __future__ import annotations

from pathlib import Path

import pytest

from src.worktrace.config import RuntimeConfig
from src.worktrace.errors import StoreWriteError
from src.worktrace.models import DayDocument, EventFileLink, WorkEvent
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
                object_hint="项目一",
                retention_reason="decision_made",
                retention_detail="张三在项目群确认主题一的处理结论。",
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
                object_hint="项目二",
                retention_reason="follow_up_assigned",
                retention_detail="李四在项目群安排继续跟进主题二。",
                source_message_ids=["om_2"],
                file_links=[],
            ),
        ],
    )

    content = Path(write_result.output_path).read_text(encoding="utf-8")

    assert "# 工作事件日报 · 2026-06-22" in content
    assert "## 事件列表" in content
    assert "<!-- worktrace:event:start event_id=\"evt1\" -->" in content
    assert "<!-- worktrace:retention_reason: decision_made -->" in content
    assert "### 1. 主题一" in content
    assert "- **日期**: 2026-06-22" in content
    assert "- **事件标题**: 主题一" in content
    assert "- **内容**: 内容一" in content
    assert "- **具体对象**: 项目一" in content
    assert "- **保留理由**: 形成明确决策" in content
    assert "- **保留依据**: 张三在项目群确认主题一的处理结论。" in content
    assert "- 来源人员:" not in content
    assert "- 来源事件 ID:" not in content
    assert "- **涉及文件**:" in content
    assert "[方案一](https://foo.feishu.cn/docx/abc)" in content
    assert "### 2. 主题二" in content
    assert "- **事件标题**: 主题二" in content
    assert "- **内容**: 内容二" in content
    assert "- **保留理由**: 形成后续跟进任务" in content
    assert "  - 无" in content
    assert "生成时间:" in content
    assert "来源: 飞书沟通记录自动整理" in content
    assert "隐私声明: 仅含与本人直接相关的工作事件，不含原始聊天记录" in content


def test_markdown_store_redacts_sensitive_link_query_params(tmp_path: Path) -> None:
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
                file_links=[
                    EventFileLink(
                        url=(
                            "https://s2adb.gydev.cn/"
                            "?password=secret-value&view=traffic&token=abc"
                        ),
                        title="",
                        link_type="normal",
                    )
                ],
            )
        ],
    )

    content = Path(write_result.output_path).read_text(encoding="utf-8")

    assert "secret-value" not in content
    assert "token=abc" not in content
    assert "password=REDACTED" in content
    assert "token=REDACTED" in content
    assert "view=traffic" in content


def test_markdown_store_redacts_internal_feishu_ids_from_public_fields(tmp_path: Path) -> None:
    store = MarkdownEventStore(config=RuntimeConfig(data_root=tmp_path / "data"))
    write_result = store.replace_day(
        "2026-06-22",
        [
            WorkEvent(
                date="2026-06-22",
                event_id="evt1",
                title="主题 om_x100b6b297bcd9080c42690b1af9082a",
                content="内容来自 oc_abc123 和 ou_user123。",
                object_hint="对象 om_x100b6b291342b4bcc49febe75b30fbd",
                retention_reason="decision_made",
                retention_detail=(
                    "张三在消息 om_x100b6b297bcd9080c42690b1af9082a 中确认结论。"
                ),
                source_message_ids=["om_x100b6b297bcd9080c42690b1af9082a"],
                file_links=[],
            )
        ],
    )

    content = Path(write_result.output_path).read_text(encoding="utf-8")

    assert "om_x100b6b297bcd9080c42690b1af9082a" not in content
    assert "om_x100b6b291342b4bcc49febe75b30fbd" not in content
    assert "oc_abc123" not in content
    assert "ou_user123" not in content
    assert "[内部消息ID已隐藏]" in content
    assert '<!-- worktrace:event:start event_id="evt1" -->' in content


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


def test_markdown_store_reads_day_when_filename_order_varies(tmp_path: Path) -> None:
    store = MarkdownEventStore(config=RuntimeConfig(data_root=tmp_path / "data"))
    manual_path = tmp_path / "data" / "2026" / "06" / "孙伟盛-2026-06-22.md"
    manual_path.parent.mkdir(parents=True, exist_ok=True)
    manual_path.write_text(
        store.render_day_document(
            DayDocument(
                date="2026-06-22",
                events=[
                    WorkEvent(
                        date="2026-06-22",
                        event_id="evt1",
                        title="主题",
                        content="内容",
                        source_message_ids=["om_1"],
                        file_links=[],
                    )
                ],
                generated_at="2026-06-22T20:00:00+08:00",
            )
        ),
        encoding="utf-8",
    )

    loaded = store.read_day("2026-06-22")

    assert loaded is not None
    assert loaded.events[0].event_id == "evt1"


def test_markdown_store_ignores_merged_markdown_when_reading_day(tmp_path: Path) -> None:
    store = MarkdownEventStore(config=RuntimeConfig(data_root=tmp_path / "data"))
    merged_path = tmp_path / "data" / "2026" / "06" / "管理者-2026-06-22-merged.md"
    merged_path.parent.mkdir(parents=True, exist_ok=True)
    merged_path.write_text(
        store.render_day_document(
            DayDocument(
                date="2026-06-22",
                events=[],
                generated_at="2026-06-22T20:00:00+08:00",
            )
        ),
        encoding="utf-8",
    )

    loaded = store.read_day("2026-06-22")

    assert loaded is None


def test_markdown_store_corrects_sentence_final_ma_in_public_fields(tmp_path: Path) -> None:
    store = MarkdownEventStore(config=RuntimeConfig(data_root=tmp_path / "data"))
    write_result = store.replace_day(
        "2026-06-22",
        [
            WorkEvent(
                date="2026-06-22",
                event_id="evt1",
                title="今天上线妈",
                content="已经同步产品妈。",
                object_hint="上线确认妈",
                retention_reason="decision_made",
                retention_detail="张三最后确认可以发版妈？",
                source_message_ids=["om_1"],
                file_links=[],
            )
        ],
    )

    content = Path(write_result.output_path).read_text(encoding="utf-8")

    assert "今天上线吗" in content
    assert "已经同步产品吗。" in content
    assert "上线确认吗" in content
    assert "张三最后确认可以发版吗？" in content


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


def test_markdown_store_roundtrip_keeps_plain_file_names(tmp_path: Path) -> None:
    store = MarkdownEventStore(config=RuntimeConfig(data_root=tmp_path / "data"))
    store.replace_day(
        "2026-06-22",
        [
            WorkEvent(
                date="2026-06-22",
                event_id="evt1",
                title="同步《友好换电管理方案.docx》",
                content="已发送《友好换电管理方案.docx》。",
                source_message_ids=["om_1"],
                file_links=[
                    EventFileLink(
                        url="",
                        title="友好换电管理方案.docx",
                        link_type="attachment",
                    )
                ],
            )
        ],
    )

    output_path = store.build_output_path("2026-06-22")
    content = output_path.read_text(encoding="utf-8")
    loaded = store.read_day("2026-06-22")

    assert "  - 《友好换电管理方案.docx》" in content
    assert loaded is not None
    assert loaded.events[0].file_links == [
        EventFileLink(
            url="",
            title="友好换电管理方案.docx",
            link_type="attachment",
        )
    ]


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


def test_markdown_store_auto_repairs_unclosed_last_event_block() -> None:
    store = MarkdownEventStore(config=RuntimeConfig())
    markdown = """---
date: 2026-07-06
event_count: 1
generated_at: 2026-07-06T10:00:00+08:00
generator: worktrace
---

# 工作事件日报 · 2026-07-06

## 事件列表

<!-- worktrace:event:start event_id="evt-bad" -->
<!-- worktrace:retention_reason: follow_up_assigned -->
### 1. 未闭合事件

- **日期**: 2026-07-06
- **事件标题**: 未闭合事件
- **内容**: 这个事件块缺少结束标记。
- **具体对象**: 解析异常
- **保留理由**: 形成后续跟进任务
- **保留依据**: 用于验证解析器能快速报错。
- **涉及文件**:
  - 无

生成时间: 2026-07-06T10:00:00+08:00
来源: 飞书沟通记录自动整理
"""

    loaded = store.parse_day_document(markdown)

    assert len(loaded.events) == 1
    assert loaded.events[0].event_id == "evt-bad"
    assert loaded.events[0].title == "未闭合事件"


def test_markdown_store_auto_repairs_missing_end_before_next_event() -> None:
    store = MarkdownEventStore(config=RuntimeConfig())
    markdown = """---
date: 2026-07-06
event_count: 2
generated_at: 2026-07-06T10:00:00+08:00
generator: worktrace
---

# 工作事件日报 · 2026-07-06

## 事件列表

<!-- worktrace:event:start event_id="evt-a" -->
<!-- worktrace:retention_reason: follow_up_assigned -->
### 1. 第一件事

- **日期**: 2026-07-06
- **事件标题**: 第一件事
- **内容**: 第一件事缺少结束标记。
- **具体对象**: 解析修复
- **保留理由**: 形成后续跟进任务
- **保留依据**: 用于验证遇到下一事件时也能自动截断。
- **涉及文件**:
  - 无

<!-- worktrace:event:start event_id="evt-b" -->
<!-- worktrace:retention_reason: decision_made -->
### 2. 第二件事

- **日期**: 2026-07-06
- **事件标题**: 第二件事
- **内容**: 第二件事格式完整。
- **具体对象**: 解析修复
- **保留理由**: 形成明确决策
- **保留依据**: 用于验证后一事件仍能正常解析。
- **涉及文件**:
  - 无
<!-- worktrace:event:end -->
"""

    loaded = store.parse_day_document(markdown)

    assert [event.event_id for event in loaded.events] == ["evt-a", "evt-b"]
    assert loaded.events[0].title == "第一件事"
    assert loaded.events[1].title == "第二件事"


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
