from __future__ import annotations

import json
from pathlib import Path

from src.worktrace.collected_merge import (
    CollectedMergeRunner,
    extract_person_name_from_filename,
)
from src.worktrace.analyzers.prompts import build_collected_merge_prompt
from src.worktrace.config import RuntimeConfig
from src.worktrace.models import (
    CollectedMergeGroup,
    CollectedMergeResult,
    CollectedSourceEvent,
    DayDocument,
)
from src.worktrace.stores.markdown import MarkdownEventStore
from src.worktrace.models import WorkEvent


class FakeAnalyzer:
    def __init__(self) -> None:
        self.calls = []

    def merge_collected_events(self, target_date, events, deterministic_groups):
        self.calls.append(
            {
                "target_date": target_date,
                "events": events,
                "deterministic_groups": deterministic_groups,
            }
        )
        return CollectedMergeResult(
            groups=[
                CollectedMergeGroup(
                    group_id="g1",
                    draft_ids=[event.draft_id for event in events],
                    title="项目排期确认",
                    content="张三和李四都确认了项目排期。",
                )
            ]
        )


def test_extract_person_name_from_date_first_filename() -> None:
    assert extract_person_name_from_filename("2026-06-29-张三.md") == "张三"
    assert extract_person_name_from_filename("张三-2026-06-29.md") == ""


def test_collected_merge_reads_sources_and_renders_source_fields(tmp_path: Path) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    inbox.mkdir(parents=True)
    source_store = MarkdownEventStore(config=RuntimeConfig(data_root=tmp_path / "unused"))
    (inbox / "2026-06-29-张三.md").write_text(
        source_store.render_day_document(
            DayDocument(
                date="2026-06-29",
                events=[
                    WorkEvent(
                        date="2026-06-29",
                        event_id="evt-shared",
                        title="排期",
                        content="确认项目排期",
                    )
                ],
                generated_at="2026-06-29T10:00:00+08:00",
            )
        ),
        encoding="utf-8",
    )
    (inbox / "2026-06-29-李四.md").write_text(
        source_store.render_day_document(
            DayDocument(
                date="2026-06-29",
                events=[
                    WorkEvent(
                        date="2026-06-29",
                        event_id="evt-shared",
                        title="排期",
                        content="确认项目排期，等待最终发布。",
                    )
                ],
                generated_at="2026-06-29T10:00:00+08:00",
            )
        ),
        encoding="utf-8",
    )
    (inbox / "_merged.md").write_text("old", encoding="utf-8")
    (inbox / "bad.md").write_text("bad", encoding="utf-8")

    analyzer = FakeAnalyzer()
    result = CollectedMergeRunner(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        analyzer=analyzer,
        cwd=tmp_path,
    ).run("2026-06-29")

    assert result.output_path == str((inbox / "_merged.md").resolve())
    assert result.source_file_count == 3
    assert result.source_event_count == 2
    assert result.skipped_file_count == 1
    assert analyzer.calls[0]["deterministic_groups"] == [
        [
            "2026-06-29-张三.md#1:evt-shared",
            "2026-06-29-李四.md#1:evt-shared",
        ]
    ]
    content = (inbox / "_merged.md").read_text(encoding="utf-8")
    assert "- 来源人员: 张三、李四" in content
    assert "- 来源事件 ID: evt-shared" in content
    assert "项目排期确认" in content


def test_collected_merge_does_not_lock_same_event_id_with_divergent_content(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    inbox.mkdir(parents=True)
    source_store = MarkdownEventStore(config=RuntimeConfig(data_root=tmp_path / "unused"))
    for person, content in [
        ("张三", "确认项目排期。"),
        ("李四", "处理客户退款审批，等待财务复核。"),
    ]:
        (inbox / f"2026-06-29-{person}.md").write_text(
            source_store.render_day_document(
                DayDocument(
                    date="2026-06-29",
                    events=[
                        WorkEvent(
                            date="2026-06-29",
                            event_id="evt-shared",
                            title="事项",
                            content=content,
                        )
                    ],
                    generated_at="2026-06-29T10:00:00+08:00",
                )
            ),
            encoding="utf-8",
        )

    analyzer = FakeAnalyzer()
    result = CollectedMergeRunner(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        analyzer=analyzer,
        cwd=tmp_path,
    ).run("2026-06-29")

    assert analyzer.calls[0]["deterministic_groups"] == []
    assert any(
        "Same event_id has divergent content: evt-shared" in warning
        for warning in result.warning_messages
    )


def test_collected_merge_empty_directory_succeeds_with_warning(tmp_path: Path) -> None:
    result = CollectedMergeRunner(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        analyzer=FakeAnalyzer(),
        cwd=tmp_path,
    ).run("2026-06-29")

    assert result.status == "success_with_warnings"
    assert result.source_event_count == 0
    assert "No valid source events found." in result.warning_messages
    assert Path(result.output_path or "").exists()


def test_collected_merge_upload_creates_date_folder_structure(tmp_path: Path) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    inbox.mkdir(parents=True)
    source_store = MarkdownEventStore(config=RuntimeConfig(data_root=tmp_path / "unused"))
    (inbox / "2026-06-29-张三.md").write_text(
        source_store.render_day_document(
            DayDocument(
                date="2026-06-29",
                events=[
                    WorkEvent(
                        date="2026-06-29",
                        event_id="evt1",
                        title="排期",
                        content="张三确认排期。",
                    )
                ],
                generated_at="2026-06-29T10:00:00+08:00",
            )
        ),
        encoding="utf-8",
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "merge_delivery.local.json").write_text(
        json.dumps({"feishu_drive_folder_url": "root-folder"}),
        encoding="utf-8",
    )
    commands = []

    def fake_command(args, *, cwd):
        commands.append(args)

        class Result:
            returncode = 0
            stdout = json.dumps({"url": f"folder-{len(commands)}"})
            stderr = ""

        return Result()

    result = CollectedMergeRunner(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        analyzer=FakeAnalyzer(),
        cwd=tmp_path,
        command_runner=fake_command,
    ).run("2026-06-29")

    assert result.upload_status == "success"
    assert [cmd[2] for cmd in commands[:3]] == [
        "+folders-create",
        "+folders-create",
        "+folders-create",
    ]
    assert [cmd[-1] for cmd in commands[:3]] == ["2026", "06", "29"]
    assert commands[-1][2] == "+upload"


def test_collected_merge_bad_upload_config_only_warns(tmp_path: Path) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    inbox.mkdir(parents=True)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "merge_delivery.local.json").write_text("{bad", encoding="utf-8")

    result = CollectedMergeRunner(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        analyzer=FakeAnalyzer(),
        cwd=tmp_path,
    ).run("2026-06-29")

    assert result.status == "success_with_warnings"
    assert result.upload_status == "failed"
    assert "Failed to upload merged markdown" in result.upload_error


def test_collected_merge_prompt_contains_sensitive_rules() -> None:
    prompt = build_collected_merge_prompt(
        "2026-06-29",
        [
            CollectedSourceEvent(
                draft_id="d1",
                person_name="张三",
                source_file="2026-06-29-张三.md",
                event=WorkEvent(
                    date="2026-06-29",
                    event_id="evt1",
                    title="排期",
                    content="确认排期。",
                ),
            )
        ],
        [],
        config=RuntimeConfig(),
    )

    assert "涉及工资、薪资、薪酬" in prompt
    assert "涉及吵架、辱骂" in prompt
    assert "不要输出对应 group" in prompt
