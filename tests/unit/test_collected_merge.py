from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.worktrace.collected_merge import (
    CollectedMergeRunner,
    aggregate_collected_quality_summaries,
    build_collected_quality_summary,
    build_grouping_summary_events,
    enforce_collected_workstream_boundaries,
    extract_person_name_from_filename,
    repair_collected_grouping_result,
)
from src.worktrace.analyzers.prompts import (
    build_collected_grouping_prompt,
    build_collected_merge_prompt,
    build_collected_render_prompt,
    build_collected_review_prompt,
)
from src.worktrace.config import EventMetadataItem, RuntimeConfig
from src.worktrace.errors import (
    AnalyzerProtocolError,
    DeliveryError,
    RetryableAnalyzerProtocolError,
)
from src.worktrace.models import (
    CollectedFactItem,
    CollectedGroupingGroup,
    CollectedGroupingResult,
    CollectedMergeGroup,
    CollectedMergeOutput,
    CollectedMergeResult,
    CollectedMergeRunResult,
    CollectedSourceEvent,
    DayDocument,
    EventFileLink,
    SelfIdentity,
)
from src.worktrace.models import WorkEvent
from src.worktrace.stores.markdown import MarkdownEventStore
from tests.helpers import NullDelivery


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
                    object_hint="项目排期",
                    retention_reason="decision_made",
                    retention_detail="多人确认项目排期结果。",
                )
            ]
        )


class TwoStageAnalyzer:
    def __init__(self, groups: list[list[str]] | str | None = None) -> None:
        self.requested_groups = groups
        self.grouping_calls = []
        self.merge_calls = []

    def group_collected_events(self, target_date, events, deterministic_groups):
        self.grouping_calls.append(
            {
                "target_date": target_date,
                "events": events,
                "deterministic_groups": deterministic_groups,
            }
        )
        groups = self.requested_groups
        if groups == "all":
            groups = [[item.draft_id for item in events]]
        if groups is None:
            groups = [[item.draft_id] for item in events]
        event_by_id = {item.draft_id: item for item in events}
        return CollectedGroupingResult(
            groups=[
                CollectedGroupingGroup(
                    group_id=f"candidate-{index}",
                    draft_ids=list(group),
                    summary_title=(
                        "多人候选事项" if len(group) > 1 else ""
                    ),
                    summary_content=(
                        "多人从不同角度补充了同一候选事项。"
                        if len(group) > 1
                        else ""
                    ),
                    summary_object_hint=(
                        event_by_id[group[0]].event.object_hint
                        if len(group) > 1
                        else ""
                    ),
                )
                for index, group in enumerate(groups, start=1)
            ]
        )

    def merge_collected_events(self, target_date, events, deterministic_groups):
        self.merge_calls.append(
            {
                "target_date": target_date,
                "events": events,
                "deterministic_groups": deterministic_groups,
            }
        )
        event_by_id = {item.draft_id: item for item in events}
        return CollectedMergeResult(
            groups=[
                CollectedMergeGroup(
                    group_id=f"rendered-{index}",
                    draft_ids=list(group),
                    title="汇总事项",
                    content="；".join(
                        event_by_id[draft_id].event.content for draft_id in group
                    ),
                    object_hint="汇总事项",
                    retention_reason="decision_made",
                    retention_detail="多人围绕同一事项形成了可追溯的沟通结论。",
                )
                for index, group in enumerate(deterministic_groups, start=1)
            ]
        )


def _event(
    *,
    event_id: str,
    title: str,
    content: str,
    object_hint: str | None = None,
    retention_reason: str = "decision_made",
    retention_detail: str | None = None,
    workstream_name: str = "",
    action_labels: list[str] | None = None,
    self_relations: list[str] | None = None,
    evidence_fingerprints: list[str] | None = None,
    conversation_fingerprints: list[str] | None = None,
    file_keys: list[str] | None = None,
) -> WorkEvent:
    return WorkEvent(
        date="2026-06-29",
        event_id=event_id,
        title=title,
        content=content,
        object_hint=object_hint or title,
        retention_reason=retention_reason,
        retention_detail=retention_detail or f"确认{title}的具体结果。",
        workstream_name=workstream_name,
        action_labels=action_labels or [],
        self_relations=self_relations or [],
        evidence_fingerprints=evidence_fingerprints or [],
        conversation_fingerprints=(
            conversation_fingerprints
            if conversation_fingerprints is not None
            else ["sha256:" + event_id.encode().hex().ljust(64, "0")[:64]]
        ),
        file_keys=file_keys or [],
    )


def _write_day_doc(path: Path, events: list[WorkEvent], tmp_path: Path) -> None:
    source_store = MarkdownEventStore(config=RuntimeConfig(data_root=tmp_path / "unused"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        source_store.render_day_document(
            DayDocument(
                date="2026-06-29",
                events=events,
                generated_at="2026-06-29T10:00:00+08:00",
            )
        ),
        encoding="utf-8",
    )


def _fake_self_identity() -> SelfIdentity:
    return SelfIdentity(open_id="ou_manager", display_name="管理者", source="test")


def _unexpected_command(args, *, cwd=None):
    raise AssertionError(f"Unexpected command: {args}")


def _build_runner(
    tmp_path: Path,
    *,
    config: RuntimeConfig | None = None,
    analyzer=None,
    delivery_channel=None,
    command_runner=None,
) -> CollectedMergeRunner:
    return CollectedMergeRunner(
        config=config or RuntimeConfig(data_root=tmp_path / "data"),
        analyzer=analyzer or FakeAnalyzer(),
        cwd=tmp_path,
        command_runner=command_runner or _unexpected_command,
        delivery_channel=delivery_channel or NullDelivery(),
        self_identity_resolver=_fake_self_identity,
    )


def test_extract_person_name_from_date_first_filename() -> None:
    assert extract_person_name_from_filename("2026-06-29-张三.md") == "张三"
    assert extract_person_name_from_filename("张三-2026-06-29.md") == "张三"
    assert extract_person_name_from_filename("张三_2026-06-29.md") == "张三"
    assert extract_person_name_from_filename("张三-2026-06-29-merged.md") == ""


def test_collected_merge_accepts_upstream_merged_markdown_and_preserves_sources(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    _write_day_doc(
        inbox / "2026-06-29-部门A-merged.md",
        [
            WorkEvent(
                date="2026-06-29",
                event_id="evt-merged",
                title="项目排期",
                content="部门A已完成项目排期确认。",
                object_hint="项目排期",
                retention_reason="decision_made",
                retention_detail="张三和李四分别确认项目排期，部门A完成汇总并形成一致结论。",
                source_people=["张三", "李四"],
                source_event_ids=["evt-a", "evt-b"],
                conversation_fingerprints=["sha256:" + "1" * 64],
            )
        ],
        tmp_path,
    )

    analyzer = FakeAnalyzer()
    result = _build_runner(tmp_path, analyzer=analyzer).run("2026-06-29")

    assert result.source_file_count == 1
    assert analyzer.calls[0]["events"][0].person_name == "部门A"
    content = Path(result.output_path or "").read_text(encoding="utf-8")
    assert "- 来源人员: 张三、李四" in content
    assert "- 来源事件 ID:" not in content
    assert '"source_event_ids":["evt-a","evt-b"]' in content
    assert "- 来源负责人: 部门A" in content
    loaded = MarkdownEventStore(config=RuntimeConfig()).parse_day_document(content)
    assert loaded.events[0].source_event_ids == ["evt-a", "evt-b"]
    assert loaded.events[0].source_report_owners == ["部门A"]


def test_collected_merge_reads_sources_and_renders_source_fields(tmp_path: Path) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    inbox.mkdir(parents=True)
    source_store = MarkdownEventStore(config=RuntimeConfig(data_root=tmp_path / "unused"))
    (inbox / "2026-06-29-张三.md").write_text(
        source_store.render_day_document(
            DayDocument(
                date="2026-06-29",
                events=[
                    _event(
                        event_id="evt-shared",
                        title="排期",
                        content="确认项目排期",
                        object_hint="项目排期",
                        retention_detail="形成项目排期确认结果。",
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
                    _event(
                        event_id="evt-shared",
                        title="排期",
                        content="确认项目排期，等待最终发布。",
                        object_hint="项目排期",
                        retention_detail="形成项目排期确认结果并明确等待最终发布。",
                    )
                ],
                generated_at="2026-06-29T10:00:00+08:00",
            )
        ),
        encoding="utf-8",
    )
    (inbox / "_merged.md").write_text("old", encoding="utf-8")
    (inbox / "2026-06-29-管理者-merge-omitted-events.md").write_text(
        "diagnostic report",
        encoding="utf-8",
    )
    (inbox / "bad.md").write_text("bad", encoding="utf-8")

    analyzer = FakeAnalyzer()
    result = _build_runner(tmp_path, analyzer=analyzer).run("2026-06-29")

    assert result.output_path == str((inbox / "2026-06-29-管理者-merged.md").resolve())
    assert result.source_file_count == 3
    assert result.source_event_count == 2
    assert result.skipped_file_count == 1
    assert result.self_delivery_status == "success"
    assert result.self_delivery_target == "ou_manager"
    assert analyzer.calls[0]["deterministic_groups"] == [
        [
            "2026-06-29-张三.md#1:evt-shared",
            "2026-06-29-李四.md#1:evt-shared",
        ]
    ]
    content = Path(result.output_path).read_text(encoding="utf-8")
    assert "- 来源人员: 张三、李四" in content
    assert "- 来源事件 ID:" not in content
    assert '"source_event_ids":["evt-shared"]' in content
    assert "- 来源负责人:" not in content
    assert "项目排期确认" in content


def test_collected_merge_skips_current_output_file_but_reads_other_merged_files(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    _write_day_doc(
        inbox / "2026-06-29-管理者-merged.md",
        [
            _event(
                event_id="evt-self",
                title="旧汇总",
                content="这是当前目录已有的旧汇总输出。",
                retention_detail="旧汇总输出不应被再次读入。",
            )
        ],
        tmp_path,
    )
    _write_day_doc(
        inbox / "2026-06-29-项目A-merged.md",
        [
            _event(
                event_id="evt-upstream",
                title="项目A事项",
                content="项目A merged 文件应继续作为输入。",
                retention_detail="项目A上游汇总文件应继续参与合并。",
            )
        ],
        tmp_path,
    )

    analyzer = FakeAnalyzer()
    result = _build_runner(tmp_path, analyzer=analyzer).run("2026-06-29")

    assert result.source_file_count == 1
    assert [event.person_name for event in analyzer.calls[0]["events"]] == ["项目A"]


def test_collected_merge_marks_merge_owner_sources(tmp_path: Path) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    _write_day_doc(
        inbox / "2026-06-29-管理者.md",
        [
            _event(
                event_id="evt-owner",
                title="版本升级",
                content="管理者确认升级到 1.0.5。",
                object_hint="WorkTrace技能1.0.5版本安装",
                retention_detail="管理者本人确认 WorkTrace 技能升级到 1.0.5。",
            )
        ],
        tmp_path,
    )
    _write_day_doc(
        inbox / "2026-06-29-张三.md",
        [
            _event(
                event_id="evt-peer",
                title="版本升级",
                content="张三提到升级到 1.0.4。",
                object_hint="WorkTrace技能1.0.4版本安装",
                retention_detail="张三提到 WorkTrace 技能升级到 1.0.4。",
            )
        ],
        tmp_path,
    )

    analyzer = FakeAnalyzer()
    result = _build_runner(tmp_path, analyzer=analyzer).run("2026-06-29")

    owner_flags = {
        event.person_name: event.is_merge_owner_source
        for event in analyzer.calls[0]["events"]
    }
    assert owner_flags == {"管理者": True, "张三": False}
    assert not any(
        "falling back to standard collected merge" in warning
        for warning in result.warning_messages
    )


def test_collected_merge_under_threshold_uses_single_analyzer_call(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    for person in ["张三", "李四", "王五"]:
        _write_day_doc(
            inbox / f"2026-06-29-{person}.md",
            [
                _event(
                    event_id=f"evt-{person}",
                    title=f"{person}排期",
                    content=f"{person}确认项目排期。",
                    object_hint=f"{person}项目排期",
                    retention_detail=f"{person}形成项目排期确认结果。",
                )
            ],
            tmp_path,
        )

    analyzer = FakeAnalyzer()
    result = _build_runner(
        tmp_path,
        analyzer=analyzer,
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            model_input_batch_target_tokens=1_000_000,
        ),
    ).run("2026-06-29")

    assert len(analyzer.calls) == 1
    assert result.merged_event_count == 1
    assert not any(
        warning.startswith("Using rolling collected merge")
        for warning in result.warning_messages
    )


def test_collected_merge_over_threshold_rolls_sources_and_keeps_provenance(
    tmp_path: Path,
) -> None:
    shared_message_fingerprint = "sha256:" + "f" * 64
    shared_file_key = "sha256:" + "e" * 64

    class ProvenanceAnalyzer(FakeAnalyzer):
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
                        group_id=f"g{len(self.calls)}",
                        draft_ids=[event.draft_id for event in events],
                        title="滚动汇总事项",
                        content="；".join(event.event.content for event in events),
                        object_hint="滚动汇总事项",
                        retention_reason="decision_made",
                        retention_detail="滚动合并保留多个来源形成的具体事项。",
                    )
                ]
            )

    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    for person in ["张三", "李四", "王五"]:
        link = EventFileLink(
            url=f"https://example.com/{person}",
            title=f"{person}文档",
            link_type="url",
        )
        _write_day_doc(
            inbox / f"2026-06-29-{person}.md",
            [
                WorkEvent(
                    date="2026-06-29",
                    event_id=f"evt-{person}",
                    title=f"{person}排期",
                    content=f"{person}确认项目排期。",
                    file_links=[link],
                    object_hint=f"{person}项目排期",
                    retention_reason="decision_made",
                    retention_detail=f"{person}形成项目排期确认结果。",
                    workstream_name="项目排期工作流",
                    action_labels=[f"{person}确认"],
                    self_relations=["collaboration"],
                    evidence_fingerprints=[
                        shared_message_fingerprint,
                        "sha256:" + person.encode().hex().ljust(64, "0")[:64],
                    ],
                    conversation_fingerprints=[
                        "sha256:" + person.encode().hex().ljust(64, "0")[:64]
                    ],
                    file_keys=[
                        shared_file_key,
                        "sha256:"
                        + (person + "file").encode().hex().ljust(64, "0")[:64],
                    ],
                )
            ],
            tmp_path,
        )

    analyzer = ProvenanceAnalyzer()
    result = _build_runner(
        tmp_path,
        analyzer=analyzer,
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            model_input_batch_target_tokens=1000,
            self_relation_types=(
                EventMetadataItem("collaboration", "协作参与", 10),
            ),
        ),
    ).run("2026-06-29")

    assert len(analyzer.calls) == 2
    assert (
        analyzer.calls[1]["events"][0].source_file
        == "__rolling_collected_merge_step_1.md"
    )
    rolling_event = analyzer.calls[1]["events"][0].event
    assert rolling_event.workstream_name == "项目排期工作流"
    assert rolling_event.action_labels
    assert rolling_event.self_relations == ["collaboration"]
    assert len(rolling_event.evidence_fingerprints) == 3
    assert len(rolling_event.file_keys) >= 3
    second_call = analyzer.calls[1]
    second_prompt = json.loads(
        build_collected_merge_prompt(
            "2026-06-29",
            second_call["events"],
            second_call["deterministic_groups"],
        )
    )
    assert second_prompt["evidence_relations"] == [
        {
            "draft_ids": sorted(
                event.draft_id for event in second_call["events"]
            ),
            "shared_message_count": 1,
            "shared_file_count": 1,
            "message_sets_equal": False,
            "file_sets_equal": False,
        }
    ]
    content = Path(result.output_path or "").read_text(encoding="utf-8")
    assert "- 来源人员: 张三、李四、王五" in content
    assert "- 来源事件 ID:" not in content
    assert '"source_event_ids":["evt-张三","evt-李四","evt-王五"]' in content
    assert "张三文档" in content
    assert "李四文档" in content
    assert "王五文档" in content
    assert "- **工作流**: 项目排期工作流" in content
    assert "- **协作方式**: 协作参与" in content
    assert any(
        warning.startswith("Using rolling collected merge:")
        and "input_target_tokens=1" in warning
        and "calls=2" in warning
        for warning in result.warning_messages
    )


def test_collected_merge_rolling_preserves_owner_signal_between_steps(
    tmp_path: Path,
) -> None:
    class OwnerCapturingAnalyzer(FakeAnalyzer):
        def merge_collected_events(self, target_date, events, deterministic_groups):
            self.calls.append(
                {
                    "target_date": target_date,
                    "events": events,
                    "deterministic_groups": deterministic_groups,
                }
            )
            owner = next(
                (event for event in events if event.is_merge_owner_source),
                events[0],
            )
            return CollectedMergeResult(
                groups=[
                    CollectedMergeGroup(
                        group_id=f"g{len(self.calls)}",
                        draft_ids=[event.draft_id for event in events],
                        title=owner.event.title,
                        content=owner.event.content,
                        object_hint=owner.event.object_hint,
                        retention_reason=owner.event.retention_reason,
                        retention_detail=owner.event.retention_detail,
                    )
                ]
            )

    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    for person, content in [
        ("管理者", "管理者确认升级到 1.0.5。"),
        ("张三", "张三反馈升级到 1.0.4。" * 20),
        ("李四", "李四反馈升级到 1.0.4。" * 30),
    ]:
        _write_day_doc(
            inbox / f"2026-06-29-{person}.md",
            [
                _event(
                    event_id=f"evt-{person}",
                    title="WorkTrace技能安装",
                    content=content,
                    object_hint="WorkTrace技能安装",
                    retention_reason="follow_up_assigned",
                    retention_detail=f"{person}确认 WorkTrace 技能安装版本信息。",
                )
            ],
            tmp_path,
        )

    analyzer = OwnerCapturingAnalyzer()
    result = _build_runner(
        tmp_path,
        analyzer=analyzer,
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            model_input_batch_target_tokens=1000,
        ),
    ).run("2026-06-29")

    assert len(analyzer.calls) == 2
    second_call_owner_events = [
        event for event in analyzer.calls[1]["events"] if event.is_merge_owner_source
    ]
    assert [event.source_file for event in second_call_owner_events] == [
        "__rolling_collected_merge_step_1.md"
    ]
    assert "1.0.5" in Path(result.output_path or "").read_text(encoding="utf-8")


def test_collected_merge_runs_root_and_first_level_subdirectories(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    _write_day_doc(
        inbox / "2026-06-29-张三.md",
        [
            _event(
                event_id="evt-root",
                title="根目录事项",
                content="张三确认根目录事项。",
                retention_detail="张三形成根目录事项确认结果。",
            )
        ],
        tmp_path,
    )
    _write_day_doc(
        inbox / "项目A" / "2026-06-29-李四.md",
        [
            _event(
                event_id="evt-a",
                title="项目A事项",
                content="李四确认项目A事项。",
                retention_detail="李四形成项目A事项确认结果。",
            )
        ],
        tmp_path,
    )
    _write_day_doc(
        inbox / "项目B" / "2026-06-29-王五.md",
        [
            _event(
                event_id="evt-b",
                title="项目B事项",
                content="王五确认项目B事项。",
                retention_detail="王五形成项目B事项确认结果。",
            )
        ],
        tmp_path,
    )

    analyzer = FakeAnalyzer()
    result = _build_runner(tmp_path, analyzer=analyzer).run("2026-06-29")

    assert len(analyzer.calls) == 3
    assert len(result.outputs) == 3
    assert result.source_file_count == 3
    assert result.source_event_count == 3
    assert result.merged_event_count == 3
    assert result.output_path == str((inbox / "2026-06-29-管理者-merged.md").resolve())
    assert (inbox / "2026-06-29-管理者-merged.md").exists()
    assert (inbox / "项目A" / "2026-06-29-管理者-merged.md").exists()
    assert (inbox / "项目B" / "2026-06-29-管理者-merged.md").exists()
    assert [output.self_delivery_status for output in result.outputs] == ["success"] * 3
    assert [Path(output.input_dir).name for output in result.outputs] == [
        "29",
        "项目A",
        "项目B",
    ]


def test_collected_merge_does_not_group_same_event_id_across_subdirectories(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    for dirname, person, title in [
        ("项目A", "张三", "项目A排期"),
        ("项目B", "李四", "项目B排期"),
    ]:
        _write_day_doc(
            inbox / dirname / f"2026-06-29-{person}.md",
            [
                _event(
                    event_id="evt-shared",
                    title=title,
                    content=f"{person}确认{title}。",
                    object_hint=title,
                    retention_detail=f"{person}形成{title}确认结果。",
                )
            ],
            tmp_path,
        )

    analyzer = FakeAnalyzer()
    result = _build_runner(tmp_path, analyzer=analyzer).run("2026-06-29")

    assert len(analyzer.calls) == 2
    assert all(call["deterministic_groups"] == [] for call in analyzer.calls)
    assert result.source_file_count == 2
    assert result.source_event_count == 2


def test_collected_merge_skips_nested_directories_inside_group(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    _write_day_doc(
        inbox / "项目A" / "2026-06-29-张三.md",
        [
            _event(
                event_id="evt-a",
                title="项目A排期",
                content="张三确认项目A排期。",
                object_hint="项目A排期",
                retention_detail="张三形成项目A排期确认结果。",
            )
        ],
        tmp_path,
    )
    (inbox / "项目A" / "更深目录").mkdir()

    result = _build_runner(tmp_path).run("2026-06-29")

    assert result.skipped_file_count == 1
    assert any(
        "Skipped nested input directory: 更深目录" in warning
        for warning in result.warning_messages
    )


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
                        _event(
                            event_id="evt-shared",
                            title="事项",
                            content=content,
                            object_hint="客户退款审批" if "客户" in content else "项目排期",
                            retention_reason=(
                                "substantive_approval" if "客户" in content else "decision_made"
                            ),
                            retention_detail=f"保留具体事项：{content}",
                        )
                    ],
                    generated_at="2026-06-29T10:00:00+08:00",
                )
            ),
            encoding="utf-8",
        )

    analyzer = FakeAnalyzer()
    result = _build_runner(tmp_path, analyzer=analyzer).run("2026-06-29")

    assert analyzer.calls[0]["deterministic_groups"] == []
    assert any(
        "Same event_id has divergent content: evt-shared" in warning
        for warning in result.warning_messages
    )


def test_collected_merge_does_not_lock_equal_fingerprint_sets(tmp_path: Path) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    shared_message = "sha256:" + "a" * 64
    shared_file = "sha256:" + "b" * 64
    for person, event_id in [("张三", "evt-a"), ("李四", "evt-b")]:
        _write_day_doc(
            inbox / f"2026-06-29-{person}.md",
            [
                _event(
                    event_id=event_id,
                    title="项目排期",
                    content=f"{person}确认项目排期。",
                    evidence_fingerprints=[shared_message],
                    file_keys=[shared_file],
                )
            ],
            tmp_path,
        )

    analyzer = FakeAnalyzer()
    _build_runner(tmp_path, analyzer=analyzer).run("2026-06-29")

    assert analyzer.calls[0]["deterministic_groups"] == []
    prompt = json.loads(
        build_collected_merge_prompt(
            "2026-06-29",
            analyzer.calls[0]["events"],
            analyzer.calls[0]["deterministic_groups"],
        )
    )
    assert prompt["evidence_relations"][0]["message_sets_equal"] is True
    assert prompt["evidence_relations"][0]["file_sets_equal"] is True


def test_collected_merge_silently_uses_standard_merge_without_owner_source(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    _write_day_doc(
        inbox / "2026-06-29-张三.md",
        [
            _event(
                event_id="evt-a",
                title="项目排期",
                content="张三确认项目排期。",
                object_hint="项目排期",
                retention_detail="张三形成项目排期确认结果。",
            )
        ],
        tmp_path,
    )

    result = _build_runner(tmp_path).run("2026-06-29")

    assert result.output_path is not None
    assert not any("merge-owner" in warning for warning in result.warning_messages)


def test_collected_merge_keeps_owner_flag_on_divergent_same_event_id(tmp_path: Path) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    for person, content in [
        ("管理者", "升级 WorkTrace 技能到 1.0.5。"),
        ("张三", "升级 WorkTrace 技能到 1.0.4。"),
    ]:
        _write_day_doc(
            inbox / f"2026-06-29-{person}.md",
            [
                _event(
                    event_id="evt-shared",
                    title="WorkTrace技能安装",
                    content=content,
                    object_hint="WorkTrace技能版本安装",
                    retention_reason="follow_up_assigned",
                    retention_detail=f"{person}确认 WorkTrace 技能安装版本信息。",
                )
            ],
            tmp_path,
        )

    analyzer = FakeAnalyzer()
    result = _build_runner(tmp_path, analyzer=analyzer).run("2026-06-29")

    assert analyzer.calls[0]["deterministic_groups"] == []
    owner_events = [
        event for event in analyzer.calls[0]["events"] if event.is_merge_owner_source
    ]
    assert [event.person_name for event in owner_events] == ["管理者"]
    assert any(
        "Same event_id has divergent content: evt-shared" in warning
        for warning in result.warning_messages
    )


def test_collected_merge_empty_directory_succeeds_with_warning(tmp_path: Path) -> None:
    result = _build_runner(tmp_path).run("2026-06-29")

    assert result.status == "success_with_warnings"
    assert result.source_event_count == 0
    assert "No valid source events found." in result.warning_messages
    assert Path(result.output_path or "").exists()


def test_collected_merge_delivers_root_result_to_self(tmp_path: Path) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    inbox.mkdir(parents=True)
    source_store = MarkdownEventStore(config=RuntimeConfig(data_root=tmp_path / "unused"))
    (inbox / "2026-06-29-张三.md").write_text(
        source_store.render_day_document(
            DayDocument(
                date="2026-06-29",
                events=[
                    _event(
                        event_id="evt1",
                        title="排期",
                        content="张三确认排期。",
                        object_hint="项目排期",
                        retention_detail="张三形成项目排期确认结果。",
                    )
                ],
                generated_at="2026-06-29T10:00:00+08:00",
            )
        ),
        encoding="utf-8",
    )

    delivered: list[str] = []

    class CapturingDelivery:
        def deliver_to_self(self, *, self_identity, markdown_path):
            delivered.append(markdown_path.name)
            return ("success", self_identity.open_id)

    result = _build_runner(
        tmp_path,
        delivery_channel=CapturingDelivery(),
    ).run("2026-06-29")

    assert result.self_delivery_status == "success"
    assert delivered == ["2026-06-29-管理者-merged.md"]


def test_collected_merge_delivers_all_outputs_to_self(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    _write_day_doc(
        inbox / "2026-06-29-张三.md",
        [
            _event(
                event_id="evt-root",
                title="根目录排期",
                content="张三确认根目录排期。",
                object_hint="根目录排期",
                retention_detail="张三形成根目录排期确认结果。",
            )
        ],
        tmp_path,
    )
    _write_day_doc(
        inbox / "项目A" / "2026-06-29-李四.md",
        [
            _event(
                event_id="evt-a",
                title="项目A排期",
                content="李四确认项目A排期。",
                object_hint="项目A排期",
                retention_detail="李四形成项目A排期确认结果。",
            )
        ],
        tmp_path,
    )
    delivered: list[str] = []

    class CapturingDelivery:
        def deliver_to_self(self, *, self_identity, markdown_path):
            delivered.append(str(markdown_path.relative_to(tmp_path)))
            return ("success", self_identity.open_id)

    result = _build_runner(
        tmp_path,
        delivery_channel=CapturingDelivery(),
    ).run("2026-06-29")

    assert result.self_delivery_status == "success"
    assert [output.self_delivery_status for output in result.outputs] == ["success", "success"]
    assert delivered == [
        "merge_inbox/2026/06/29/2026-06-29-管理者-merged.md",
        "merge_inbox/2026/06/29/项目A/2026-06-29-管理者-merged.md",
    ]


def test_collected_merge_delivery_failure_only_warns(tmp_path: Path) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    inbox.mkdir(parents=True)

    class FailingDelivery:
        def deliver_to_self(self, *, self_identity, markdown_path):
            raise DeliveryError("delivery failed")

    result = _build_runner(
        tmp_path,
        delivery_channel=FailingDelivery(),
    ).run("2026-06-29")

    assert result.status == "success_with_warnings"
    assert result.self_delivery_status == "failed"
    assert result.self_delivery_target == "ou_manager"
    assert "delivery failed" in result.self_delivery_error


def test_collected_merge_prompt_contains_sensitive_rules() -> None:
    prompt = build_collected_merge_prompt(
        "2026-06-29",
        [
            CollectedSourceEvent(
                draft_id="d1",
                person_name="张三",
                source_file="2026-06-29-张三.md",
                is_merge_owner_source=True,
                event=WorkEvent(
                    date="2026-06-29",
                    event_id="evt1",
                    title="排期",
                    content="确认排期。",
                    object_hint="项目排期",
                    retention_reason="decision_made",
                    retention_detail="形成项目排期确认结果。",
                ),
            )
        ],
        [],
        config=RuntimeConfig(
            sensitive_event_keywords=("工资", "薪资", "薪酬", "吵架", "辱骂"),
        ),
    )

    payload = json.loads(prompt)

    assert payload["merge_owner_person"] == "张三"
    assert payload["remaining_events"][0]["is_merge_owner_source"] is True
    assert payload["remaining_events"][0]["source_people"] == []
    assert payload["remaining_events"][0]["source_event_ids"] == []
    assert "涉及工资、薪资、薪酬、吵架、辱骂" in prompt
    assert "不要输出对应 group" in prompt
    assert "retention_reason" in prompt
    assert "只有不同来源" in prompt
    assert "存在明确冲突" in prompt
    assert "最终 group 必须以 1.0.5 为主事实" in prompt
    assert "workstream_name 相同只表示可能属于同一工作范围" in prompt
    assert "只有标题相似、时间接近或部门相同，不能作为合并依据" in prompt


def test_collected_merge_prompt_includes_python_evidence_relations() -> None:
    message_a = "sha256:" + "a" * 64
    message_b = "sha256:" + "b" * 64
    message_c = "sha256:" + "c" * 64
    file_a = "sha256:" + "d" * 64
    prompt = build_collected_merge_prompt(
        "2026-06-29",
        [
            CollectedSourceEvent(
                draft_id="d2",
                person_name="李四",
                source_file="2026-06-29-李四.md",
                event=_event(
                    event_id="evt2",
                    title="项目甲方案补充",
                    content="补充确认项目甲方案。",
                    workstream_name="项目甲",
                    evidence_fingerprints=[message_a, message_b],
                    file_keys=[file_a],
                ),
            ),
            CollectedSourceEvent(
                draft_id="d1",
                person_name="张三",
                source_file="2026-06-29-张三.md",
                event=_event(
                    event_id="evt1",
                    title="项目甲方案",
                    content="确认项目甲方案。",
                    workstream_name="项目甲",
                    action_labels=["方案确认"],
                    self_relations=["initiated"],
                    evidence_fingerprints=[message_a, message_b, message_b],
                    file_keys=[file_a, file_a],
                ),
            ),
            CollectedSourceEvent(
                draft_id="d3",
                person_name="王五",
                source_file="2026-06-29-王五.md",
                event=_event(
                    event_id="evt3",
                    title="项目甲排期",
                    content="确认项目甲上线排期。",
                    workstream_name="项目甲",
                    evidence_fingerprints=[message_a, message_c],
                ),
            )
        ],
        [],
        config=RuntimeConfig(
            self_relation_types=(EventMetadataItem("initiated", "发起", 10),),
        ),
    )

    payload = json.loads(prompt)
    event_payload = next(
        item for item in payload["remaining_events"] if item["draft_id"] == "d1"
    )

    assert event_payload["workstream_name"] == "项目甲"
    assert event_payload["action_labels"] == ["方案确认"]
    assert event_payload["self_relations"] == [{"key": "initiated", "label": "发起"}]
    assert "evidence_fingerprints" not in event_payload
    assert "file_keys" not in event_payload
    assert payload["evidence_relations"] == [
        {
            "draft_ids": ["d1", "d2"],
            "shared_message_count": 2,
            "shared_file_count": 1,
            "message_sets_equal": True,
            "file_sets_equal": True,
        },
        {
            "draft_ids": ["d1", "d3"],
            "shared_message_count": 1,
            "shared_file_count": 0,
            "message_sets_equal": False,
            "file_sets_equal": False,
        },
        {
            "draft_ids": ["d2", "d3"],
            "shared_message_count": 1,
            "shared_file_count": 0,
            "message_sets_equal": False,
            "file_sets_equal": False,
        },
    ]
    assert message_a not in prompt
    assert message_b not in prompt
    assert message_c not in prompt
    assert file_a not in prompt


def test_collected_content_prompts_require_specific_event_titles() -> None:
    events = [
        CollectedSourceEvent(
            "d1",
            "张三",
            "a.md",
            _event(
                event_id="e1",
                title="项目风险反馈",
                content="反馈项目方案尚未确定。",
                object_hint="项目方案",
            ),
        )
    ]

    merge_payload = json.loads(
        build_collected_merge_prompt("2026-06-29", events, [])
    )
    render_payload = json.loads(
        build_collected_render_prompt("2026-06-29", events, [["d1"]])
    )

    assert any(
        "本组每个 draft_id 至少要在一项 fact_item" in rule
        for rule in render_payload["rules"]
    )

    for payload in (merge_payload, render_payload):
        assert any(
            "具体对象 + 关键动作、进展、结果或风险" in rule
            for rule in payload["rules"]
        )
        assert any(
            "不得只写无法区分实际事项的通用类别" in rule
            for rule in payload["rules"]
        )


def test_collected_merge_prompt_handles_file_only_and_empty_evidence() -> None:
    file_a = "sha256:" + "a" * 64
    file_b = "sha256:" + "b" * 64
    events = [
        CollectedSourceEvent(
            "d1",
            "张三",
            "a.md",
            _event(
                event_id="e1",
                title="文件核对",
                content="核对文件。",
                file_keys=[file_a, file_b],
            ),
        ),
        CollectedSourceEvent(
            "d2",
            "李四",
            "b.md",
            _event(
                event_id="e2",
                title="文件复核",
                content="复核文件。",
                file_keys=[file_a],
            ),
        ),
        CollectedSourceEvent(
            "d3",
            "王五",
            "c.md",
            _event(event_id="e3", title="其他事项", content="处理其他事项。"),
        ),
        CollectedSourceEvent(
            "d4",
            "赵六",
            "d.md",
            _event(event_id="e4", title="空证据事项", content="处理空证据事项。"),
        ),
    ]

    payload = json.loads(build_collected_merge_prompt("2026-06-29", events, []))

    assert payload["evidence_relations"] == [
        {
            "draft_ids": ["d1", "d2"],
            "shared_message_count": 0,
            "shared_file_count": 1,
            "message_sets_equal": False,
            "file_sets_equal": False,
        }
    ]


def test_collected_merge_splits_different_named_workstreams() -> None:
    source_events = [
        CollectedSourceEvent("d1", "张三", "a.md", _event(
            event_id="e1",
            title="项目甲",
            content="推进项目甲。",
            workstream_name="项目甲",
        )),
        CollectedSourceEvent("d2", "李四", "b.md", _event(
            event_id="e2",
            title="项目乙",
            content="推进项目乙。",
            workstream_name="项目乙",
        )),
    ]
    result, warnings = enforce_collected_workstream_boundaries(
        CollectedMergeResult(
            groups=[
                CollectedMergeGroup(
                    group_id="bad-group",
                    draft_ids=["d1", "d2"],
                    title="错误合并",
                    content="错误合并内容。",
                )
            ]
        ),
        source_events,
    )

    assert [group.draft_ids for group in result.groups] == [["d1"], ["d2"]]
    assert len(warnings) == 1


def test_collected_merge_does_not_append_source_bodies_to_model_content(
    tmp_path: Path,
) -> None:
    source_events = [
        CollectedSourceEvent(
            "d1",
            "管理者",
            "a.md",
            _event(
                event_id="e1",
                title="项目方案",
                content="管理者确认了方案范围。",
            ),
            is_merge_owner_source=True,
        ),
        CollectedSourceEvent("d2", "李四", "b.md", _event(
            event_id="e2",
            title="项目方案",
            content="李四完成了执行验证，并记录待办。",
        )),
    ]
    runner = _build_runner(tmp_path)
    result, warnings = runner._fill_collected_merge_group_metadata(
        source_events,
        CollectedMergeResult(
            groups=[
                CollectedMergeGroup(
                    group_id="g1",
                    draft_ids=["d1", "d2"],
                    title="项目方案",
                    content="管理者确认了方案范围。",
                    object_hint="项目方案",
                    retention_reason="decision_made",
                    retention_detail="形成方案结论并完成验证。",
                )
            ]
        ),
    )

    assert "管理者确认了方案范围" in result.groups[0].content
    assert "李四完成了执行验证" not in result.groups[0].content
    assert warnings == []


def test_collected_merge_owner_wins_only_when_conflict_is_marked(tmp_path: Path) -> None:
    source_events = [
        CollectedSourceEvent(
            "d1",
            "管理者",
            "owner.md",
            _event(event_id="e1", title="版本升级", content="升级到 1.0.5。"),
            is_merge_owner_source=True,
        ),
        CollectedSourceEvent(
            "d2",
            "张三",
            "staff.md",
            _event(event_id="e2", title="版本升级", content="升级到 1.0.4。"),
        ),
    ]
    runner = _build_runner(tmp_path)
    result, warnings = runner._fill_collected_merge_group_metadata(
        source_events,
        CollectedMergeResult(
            groups=[
                CollectedMergeGroup(
                    group_id="g1",
                    draft_ids=["d1", "d2"],
                    title="版本升级",
                    content="升级到 1.0.5。",
                    object_hint="版本升级",
                    retention_reason="decision_made",
                    retention_detail="已确认最终升级版本。",
                    merge_owner_conflict=True,
                    conflict_detail="来源版本号不一致。",
                )
            ]
        ),
    )

    assert result.groups[0].content == "升级到 1.0.5。"
    assert any("Resolved explicit source conflict" in warning for warning in warnings)


def test_collected_merge_owner_signal_can_drive_final_version_output(
    tmp_path: Path,
) -> None:
    class OwnerPreferringAnalyzer(FakeAnalyzer):
        def merge_collected_events(self, target_date, events, deterministic_groups):
            self.calls.append(
                {
                    "target_date": target_date,
                    "events": events,
                    "deterministic_groups": deterministic_groups,
                }
            )
            owner_event = next(event for event in events if event.is_merge_owner_source)
            peer_events = [event for event in events if not event.is_merge_owner_source]
            peer_clause = ""
            if peer_events:
                peer_clause = f"；其他成员也已反馈安装情况：{peer_events[0].event.content}"
            return CollectedMergeResult(
                groups=[
                    CollectedMergeGroup(
                        group_id="g1",
                        draft_ids=[event.draft_id for event in events],
                        title=owner_event.event.title,
                        content=owner_event.event.content + peer_clause,
                        object_hint=owner_event.event.object_hint,
                        retention_reason=owner_event.event.retention_reason,
                        retention_detail=owner_event.event.retention_detail,
                    )
                ]
            )

    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    _write_day_doc(
        inbox / "2026-06-29-张三.md",
        [
            _event(
                event_id="evt-staff",
                title="WorkTrace技能安装",
                content="普通员工提到升级到 1.0.4。",
                object_hint="WorkTrace技能1.0.4版本安装",
                retention_reason="follow_up_assigned",
                retention_detail="普通员工提到 WorkTrace 技能升级到 1.0.4。",
            )
        ],
        tmp_path,
    )
    _write_day_doc(
        inbox / "2026-06-29-管理者.md",
        [
            _event(
                event_id="evt-owner",
                title="WorkTrace技能安装",
                content="管理者要求全体成员升级到 1.0.5，并生成30日事件文件。",
                object_hint="WorkTrace技能1.0.5版本安装与30日事件文件生成",
                retention_reason="follow_up_assigned",
                retention_detail="管理者本人明确要求升级到 1.0.5 并产出30日事件文件。",
            )
        ],
        tmp_path,
    )

    analyzer = OwnerPreferringAnalyzer()
    result = _build_runner(tmp_path, analyzer=analyzer).run("2026-06-29")

    assert any(event.is_merge_owner_source for event in analyzer.calls[0]["events"])
    content = Path(result.output_path or "").read_text(encoding="utf-8")
    assert "1.0.5" in content
    assert "WorkTrace技能1.0.5版本安装与30日事件文件生成" in content


def test_collected_merge_filters_low_retention_source_events_before_prompt(
    tmp_path: Path,
) -> None:
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
                        event_id="evt-low",
                        title="下午会议安排",
                        content="确认下午2点开会互通信息。",
                        object_hint="会议",
                        retention_reason="decision_made",
                        retention_detail="确认下午2点开会互通信息。",
                        conversation_fingerprints=["sha256:" + "2" * 64],
                    ),
                    _event(
                        event_id="evt-keep",
                        title="需求评审",
                        content="确认需求变更范围和上线排期。",
                        object_hint="需求变更范围和上线排期",
                        retention_reason="decision_made",
                        retention_detail="评审形成需求变更范围和上线排期结论。",
                    ),
                ],
                generated_at="2026-06-29T10:00:00+08:00",
            )
        ),
        encoding="utf-8",
    )

    analyzer = FakeAnalyzer()
    result = _build_runner(tmp_path, analyzer=analyzer).run("2026-06-29")

    assert result.source_event_count == 1
    assert [event.event.title for event in analyzer.calls[0]["events"]] == ["需求评审"]
    content = (inbox / "2026-06-29-管理者-merged.md").read_text(encoding="utf-8")
    assert "下午会议安排" not in content


def test_collected_merge_filters_sensitive_source_events_before_prompt(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    _write_day_doc(
        inbox / "2026-06-29-张三.md",
        [
            _event(
                event_id="evt-sensitive",
                title="薪资调整审批",
                content="确认员工薪资调整方案。",
                object_hint="薪资调整",
                retention_detail="审批通过薪资调整。",
            ),
            _event(
                event_id="evt-keep",
                title="需求评审",
                content="确认需求变更范围和上线排期。",
                object_hint="需求变更范围和上线排期",
                retention_detail="评审形成需求变更范围和上线排期结论。",
            ),
        ],
        tmp_path,
    )

    analyzer = FakeAnalyzer()
    result = _build_runner(
        tmp_path,
        analyzer=analyzer,
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            sensitive_event_keywords=("薪资",),
            collected_merge_trace_enabled=True,
            collected_merge_trace_root=tmp_path / "trace",
        ),
    ).run("2026-06-29")

    assert result.source_event_count == 1
    assert [event.event.title for event in analyzer.calls[0]["events"]] == ["需求评审"]
    assert (
        "Filtered sensitive source event: "
        "2026-06-29-张三.md#evt-sensitive (薪资调整审批)."
    ) in result.warning_messages
    source_audit = json.loads(
        (tmp_path / "trace" / "2026-06-29" / "source-audit.json").read_text(
            encoding="utf-8"
        )
    )
    assert source_audit["filter_diagnostics"] == [
        {
            "event_id": "evt-sensitive",
            "event_title": "薪资调整审批",
            "kind": "sensitive",
            "source_file": "2026-06-29-张三.md",
            "source_person": "张三",
            "stage": "source_filter",
        }
    ]
    content = (inbox / "2026-06-29-管理者-merged.md").read_text(encoding="utf-8")
    assert "薪资" not in content


def test_collected_merge_fills_group_missing_retention_metadata_from_sources(
    tmp_path: Path,
) -> None:
    class MissingRetentionAnalyzer(FakeAnalyzer):
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
                        title="需求评审",
                        content="确认需求变更范围和上线排期。",
                    )
                ]
            )

    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    inbox.mkdir(parents=True)
    source_store = MarkdownEventStore(config=RuntimeConfig(data_root=tmp_path / "unused"))
    (inbox / "2026-06-29-张三.md").write_text(
        source_store.render_day_document(
            DayDocument(
                date="2026-06-29",
                events=[
                    _event(
                        event_id="evt-keep",
                        title="需求评审",
                        content="确认需求变更范围和上线排期。",
                        object_hint="需求变更范围和上线排期",
                        retention_reason="decision_made",
                        retention_detail="评审形成需求变更范围和上线排期结论。",
                    )
                ],
                generated_at="2026-06-29T10:00:00+08:00",
            )
        ),
        encoding="utf-8",
    )

    result = _build_runner(tmp_path, analyzer=MissingRetentionAnalyzer()).run("2026-06-29")

    assert result.merged_event_count == 1
    assert any(
        "Filled collected merge metadata from source events: 需求评审"
        in warning
        for warning in result.warning_messages
    )
    content = (inbox / "2026-06-29-管理者-merged.md").read_text(encoding="utf-8")
    assert "评审形成需求变更范围和上线排期结论。" in content


def test_collected_merge_retries_structural_missing_retention_detail(
    tmp_path: Path,
) -> None:
    class RetryAnalyzer(FakeAnalyzer):
        def merge_collected_events(self, target_date, events, deterministic_groups):
            self.calls.append(
                {
                    "target_date": target_date,
                    "events": events,
                    "deterministic_groups": deterministic_groups,
                }
            )
            if len(self.calls) == 1:
                return CollectedMergeResult(
                    groups=[
                        CollectedMergeGroup(
                            group_id="g1",
                            draft_ids=[events[0].draft_id],
                            title="需求评审",
                            content="确认需求变更范围和上线排期。",
                            object_hint="需求变更范围和上线排期",
                            retention_reason="decision_made",
                            retention_detail="",
                        ),
                        CollectedMergeGroup(
                            group_id="g2",
                            draft_ids=[events[1].draft_id],
                            title="发布排期",
                            content="确认发布排期。",
                            object_hint="发布排期",
                            retention_reason="follow_up_assigned",
                            retention_detail="",
                        ),
                    ]
                )
            return CollectedMergeResult(
                groups=[
                    CollectedMergeGroup(
                        group_id="g1",
                        draft_ids=[event.draft_id for event in events],
                        title="需求评审与发布排期",
                        content="确认需求变更范围、上线排期和发布排期。",
                        object_hint="需求评审与发布排期",
                        retention_reason="decision_made",
                        retention_detail="评审形成需求变更范围、上线排期和发布排期结论。",
                    )
                ]
            )

    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    _write_day_doc(
        inbox / "2026-06-29-张三.md",
        [
            _event(
                event_id="evt-1",
                title="需求评审",
                content="确认需求变更范围和上线排期。",
                object_hint="需求变更范围和上线排期",
                retention_detail="评审形成需求变更范围和上线排期结论。",
            ),
            _event(
                event_id="evt-2",
                title="发布排期",
                content="确认发布排期。",
                object_hint="发布排期",
                retention_reason="follow_up_assigned",
                retention_detail="确认发布排期并形成后续跟进计划。",
            ),
        ],
        tmp_path,
    )

    analyzer = RetryAnalyzer()
    result = _build_runner(
        tmp_path,
        analyzer=analyzer,
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            collected_merge_missing_field_retry_ratio=0.2,
            collected_merge_missing_field_retry_limit=1,
        ),
    ).run("2026-06-29")

    assert len(analyzer.calls) == 2
    assert result.merged_event_count == 1
    assert any(
        warning.startswith("Retrying collected merge because required fields were missing")
        for warning in result.warning_messages
    )
    content = (inbox / "2026-06-29-管理者-merged.md").read_text(encoding="utf-8")
    assert "评审形成需求变更范围、上线排期和发布排期结论。" in content


def test_collected_merge_partially_reads_truncated_source_file(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    source_path = inbox / "2026-06-29-张三.md"
    _write_day_doc(
        source_path,
        [
            _event(
                event_id="evt-complete",
                title="完整事件",
                content="确认完整事件的执行结果。",
            )
        ],
        tmp_path,
    )
    original = source_path.read_text(encoding="utf-8").replace(
        "event_count: 1",
        "event_count: 2",
    )
    original += '\n<!-- worktrace:event:start event_id="evt-truncated" -->\n'
    source_path.write_text(original, encoding="utf-8")

    trace_root = tmp_path / "trace"
    result = _build_runner(
        tmp_path,
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            collected_merge_trace_enabled=True,
            collected_merge_trace_root=trace_root,
        ),
    ).run("2026-06-29")

    assert result.partial_file_count == 1
    assert result.skipped_file_count == 0
    assert result.source_event_count == 1
    assert source_path.read_text(encoding="utf-8") == original
    assert any(
        "Partially read source markdown: 2026-06-29-张三.md" in warning
        and "skipped_event_ids=evt-truncated" in warning
        for warning in result.warning_messages
    )
    source_audit = json.loads(
        (trace_root / "2026-06-29" / "source-audit.json").read_text(
            encoding="utf-8"
        )
    )
    assert source_audit["partial_file_count"] == 1
    assert source_audit["source_files"][0]["status"] == "partial"
    assert source_audit["source_files"][0]["partial_event_ids"] == [
        "evt-truncated"
    ]


def test_collected_merge_does_not_retry_non_retryable_error(tmp_path: Path) -> None:
    class NonRetryableAnalyzer:
        def __init__(self) -> None:
            self.call_count = 0

        def merge_collected_events(self, target_date, events, deterministic_groups):
            self.call_count += 1
            raise AnalyzerProtocolError("HTTP 401: invalid API key")

    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    _write_day_doc(
        inbox / "2026-06-29-张三.md",
        [_event(event_id="evt-1", title="项目排期", content="确认项目排期。")],
        tmp_path,
    )
    analyzer = NonRetryableAnalyzer()
    trace_root = tmp_path / "trace"

    result = _build_runner(
        tmp_path,
        analyzer=analyzer,
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            collected_merge_trace_enabled=True,
            collected_merge_trace_root=trace_root,
        ),
    ).run("2026-06-29")

    assert result.status == "failed"
    assert analyzer.call_count == 1
    step = json.loads(
        (trace_root / "2026-06-29" / "step-001.json").read_text(
            encoding="utf-8"
        )
    )
    assert step["status"] == "failed"
    assert step["error"]["retryable"] is False


def test_collected_merge_does_not_retry_retryable_error(tmp_path: Path) -> None:
    class RetryableFailureAnalyzer:
        def __init__(self) -> None:
            self.call_count = 0

        def merge_collected_events(self, target_date, events, deterministic_groups):
            self.call_count += 1
            raise RetryableAnalyzerProtocolError("temporary network failure")

    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    _write_day_doc(
        inbox / "2026-06-29-张三.md",
        [_event(event_id="evt-1", title="项目排期", content="确认项目排期。")],
        tmp_path,
    )
    analyzer = RetryableFailureAnalyzer()

    result = _build_runner(tmp_path, analyzer=analyzer).run("2026-06-29")

    assert result.status == "failed"
    assert analyzer.call_count == 1


def test_collected_merge_mixes_enhanced_legacy_and_upstream_sources(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    _write_day_doc(
        inbox / "2026-06-29-新流程.md",
        [
            _event(
                event_id="evt-new",
                title="新流程事项",
                content="确认新流程事项。",
                workstream_name="项目工作流",
                self_relations=["collaboration"],
                evidence_fingerprints=["sha256:" + "a" * 64],
            )
        ],
        tmp_path,
    )
    _write_day_doc(
        inbox / "2026-06-29-旧流程.md",
        [_event(event_id="evt-old", title="旧流程事项", content="确认旧流程事项。")],
        tmp_path,
    )
    _write_day_doc(
        inbox / "2026-06-29-上游-merged.md",
        [
            replace(
                _event(
                    event_id="evt-upstream",
                    title="上游汇总事项",
                    content="确认上游汇总事项。",
                ),
                source_people=["甲", "乙"],
                source_event_ids=["evt-a", "evt-b"],
            )
        ],
        tmp_path,
    )
    analyzer = FakeAnalyzer()
    trace_root = tmp_path / "trace"

    result = _build_runner(
        tmp_path,
        analyzer=analyzer,
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            collected_merge_trace_enabled=True,
            collected_merge_trace_root=trace_root,
        ),
    ).run("2026-06-29")

    assert result.source_file_count == 3
    source_by_file = {item.source_file: item for item in analyzer.calls[0]["events"]}
    assert source_by_file["2026-06-29-新流程.md"].event.self_relations == [
        "collaboration"
    ]
    assert source_by_file["2026-06-29-旧流程.md"].event.self_relations == []
    assert source_by_file["2026-06-29-上游-merged.md"].event.source_people == [
        "甲",
        "乙",
    ]
    source_audit = json.loads(
        (trace_root / "2026-06-29" / "source-audit.json").read_text(
            encoding="utf-8"
        )
    )
    assert {item["source_file"]: item["format"] for item in source_audit["source_files"]} == {
        "2026-06-29-上游-merged.md": "upstream_merged",
        "2026-06-29-新流程.md": "enhanced_personal",
        "2026-06-29-旧流程.md": "legacy_personal",
    }


def test_collected_merge_trace_writes_step_summary(tmp_path: Path) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    _write_day_doc(
        inbox / "2026-06-29-张三.md",
        [
            _event(
                event_id="evt-1",
                title="需求评审",
                content="确认需求变更范围和上线排期。",
                object_hint="需求变更范围和上线排期",
                retention_detail="评审形成需求变更范围和上线排期结论。",
                workstream_name="需求评审工作流",
                action_labels=["范围确认"],
                self_relations=["decision_confirmation"],
                evidence_fingerprints=["sha256:" + "a" * 64],
                file_keys=["sha256:" + "b" * 64],
            )
        ],
        tmp_path,
    )

    trace_root = tmp_path / "trace"
    result = _build_runner(
        tmp_path,
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            collected_merge_trace_enabled=True,
            collected_merge_trace_root=trace_root,
        ),
    ).run("2026-06-29")

    assert result.merged_event_count == 1
    summary_path = trace_root / "2026-06-29" / "summary.json"
    step_path = trace_root / "2026-06-29" / "step-001.json"
    prompt_path = trace_root / "2026-06-29" / "step-001-prompt.txt"
    assert summary_path.exists()
    assert step_path.exists()
    assert prompt_path.exists()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["merged_event_count"] == 1
    step = json.loads(step_path.read_text(encoding="utf-8"))
    assert step["raw_group_count"] == 1
    assert step["retained_metrics"]["event_count"] == 1
    assert len(step["input_events"]) == 1
    input_event = step["input_events"][0]["event"]
    assert input_event["workstream_name"] == "需求评审工作流"
    assert input_event["action_labels"] == ["范围确认"]
    assert input_event["self_relations"] == ["decision_confirmation"]
    assert input_event["evidence_fingerprints"] == ["sha256:" + "a" * 64]
    assert input_event["file_keys"] == ["sha256:" + "b" * 64]
    assert "sha256:" not in prompt_path.read_text(encoding="utf-8")
    assert step["deterministic_groups"] == []
    assert step["boundary_warnings"] == []


def test_collected_merge_trace_records_workstream_boundary_warnings(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    _write_day_doc(
        inbox / "2026-06-29-张三.md",
        [
            _event(
                event_id="evt-a",
                title="项目甲推进",
                content="推进项目甲。",
                workstream_name="项目甲",
            )
        ],
        tmp_path,
    )
    _write_day_doc(
        inbox / "2026-06-29-李四.md",
        [
            _event(
                event_id="evt-b",
                title="项目乙推进",
                content="推进项目乙。",
                workstream_name="项目乙",
            )
        ],
        tmp_path,
    )
    trace_root = tmp_path / "trace"

    result = _build_runner(
        tmp_path,
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            collected_merge_trace_enabled=True,
            collected_merge_trace_root=trace_root,
        ),
    ).run("2026-06-29")

    assert result.merged_event_count == 2
    step = json.loads(
        (trace_root / "2026-06-29" / "step-001.json").read_text(encoding="utf-8")
    )
    assert len(step["input_events"]) == 2
    assert step["boundary_warnings"] == [
        "Split collected merge group because different named workstreams cannot merge: g1."
    ]


def test_collected_merge_run_result_round_trips_outputs(tmp_path: Path) -> None:
    result = CollectedMergeRunResult(
        status="success",
        target_date="2026-06-29",
        input_dir=str(tmp_path / "merge_inbox/2026/06/29"),
        output_path=str(tmp_path / "merge_inbox/2026/06/29/2026-06-29-管理者-merged.md"),
        source_file_count=3,
        source_event_count=4,
        merged_event_count=2,
        skipped_file_count=1,
        partial_file_count=1,
        warning_messages=[],
        self_delivery_status="success",
        self_delivery_target="ou_manager",
        outputs=[
            CollectedMergeOutput(
                input_dir=str(tmp_path / "merge_inbox/2026/06/29/项目A"),
                output_path=str(
                    tmp_path / "merge_inbox/2026/06/29/项目A/2026-06-29-管理者-merged.md"
                ),
                source_file_count=1,
                source_event_count=2,
                merged_event_count=1,
                skipped_file_count=0,
                partial_file_count=1,
                warning_messages=["warning"],
                self_delivery_status="success",
            )
        ],
    )

    payload = result.to_dict()
    restored = CollectedMergeRunResult.from_dict(payload)

    assert payload["outputs"][0]["source_event_count"] == 2
    assert payload["partial_file_count"] == 1
    assert restored.outputs[0].partial_file_count == 1
    assert restored.outputs[0].warning_messages == ["warning"]


def test_collected_grouping_prompt_uses_same_day_conversation_candidates() -> None:
    conversation = "sha256:" + "c" * 64
    message_a = "sha256:" + "a" * 64
    message_b = "sha256:" + "b" * 64
    long_content = "张三提出完整方案并补充执行细节。" * 30
    events = [
        CollectedSourceEvent(
            draft_id="d1",
            person_name="张三",
            source_file="a.md",
            event=_event(
                event_id="e1",
                title="方案确认",
                content=long_content,
                evidence_fingerprints=[message_a],
                conversation_fingerprints=[conversation],
            ),
        ),
        CollectedSourceEvent(
            draft_id="d2",
            person_name="李四",
            source_file="b.md",
            event=_event(
                event_id="e2",
                title="方案反馈",
                content="李四反馈方案。",
                evidence_fingerprints=[message_b],
                conversation_fingerprints=[conversation],
            ),
        ),
    ]

    prompt = build_collected_grouping_prompt(
        "2026-06-29",
        events,
        [],
        config=RuntimeConfig(prompt_message_char_limit=12),
    )
    payload = json.loads(prompt)

    assert payload["conversation_groups"] == [
        {"group_id": "conversation-001", "draft_ids": ["d1", "d2"]}
    ]
    assert payload["evidence_relations"] == []
    assert conversation not in prompt
    assert message_a not in prompt
    assert message_b not in prompt
    assert payload["events"][0]["content"] == long_content
    assert payload["required_output_schema"]["split_reason"].startswith("empty")
    assert "same_deliverable_batch" in payload["group_reason_definitions"]
    assert "不同文件" in payload["group_reason_definitions"]["shared_file"]


def test_grouping_repair_preserves_only_matching_candidate_summary() -> None:
    events = [
        CollectedSourceEvent(
            draft_id="d1",
            person_name="张三",
            source_file="a.md",
            event=_event(event_id="e1", title="方案", content="提出方案。"),
        ),
        CollectedSourceEvent(
            draft_id="d2",
            person_name="李四",
            source_file="b.md",
            event=_event(event_id="e2", title="反馈", content="反馈方案。"),
        ),
    ]
    valid = CollectedGroupingResult(
        groups=[
            CollectedGroupingGroup(
                group_id="g1",
                draft_ids=["d1", "d2"],
                summary_title="方案确认",
                summary_content="提出方案并完成反馈。",
                summary_object_hint="方案",
            )
        ]
    )

    repaired, warnings = repair_collected_grouping_result(valid, events, [])

    assert warnings == []
    assert repaired.groups[0].summary_content == "提出方案并完成反馈。"
    assert repaired.groups[0].summary_source == "model"

    changed = replace(
        valid,
        groups=[replace(valid.groups[0], draft_ids=["d1", "d2", "unknown"])],
    )
    repaired_changed, changed_warnings = repair_collected_grouping_result(
        changed,
        events,
        [],
    )

    assert repaired_changed.groups[0].summary_content == ""
    assert repaired_changed.groups[0].summary_source == "balanced_fallback"
    assert any("Discarded collected candidate summaries" in item for item in changed_warnings)


def test_grouping_summary_event_uses_model_summary_and_keeps_evidence() -> None:
    conversation = "sha256:" + "c" * 64
    events = [
        CollectedSourceEvent(
            draft_id="d1",
            person_name="张三",
            source_file="a.md",
            event=_event(
                event_id="e1",
                title="方案提出",
                content="张三提出价格方案。",
                action_labels=["提出"],
                conversation_fingerprints=[conversation],
            ),
        ),
        CollectedSourceEvent(
            draft_id="d2",
            person_name="李四",
            source_file="b.md",
            event=_event(
                event_id="e2",
                title="方案反馈",
                content="李四反馈执行影响。",
                action_labels=["反馈"],
                conversation_fingerprints=[conversation],
            ),
        ),
    ]
    group = CollectedGroupingGroup(
        group_id="g1",
        draft_ids=["d1", "d2"],
        summary_title="价格方案评估",
        summary_content="提出价格方案并反馈执行影响。",
        summary_object_hint="价格方案",
        summary_source="model",
    )

    summaries, source_ids = build_grouping_summary_events(
        "2026-06-29",
        events,
        [group],
        depth=1,
    )

    assert source_ids[summaries[0].draft_id] == ["d1", "d2"]
    assert summaries[0].candidate_summary_source == "model"
    assert summaries[0].event.content == "提出价格方案并反馈执行影响。"
    assert summaries[0].event.source_people == ["张三", "李四"]
    assert summaries[0].event.source_event_ids == ["e1", "e2"]
    assert summaries[0].event.action_labels == ["提出", "反馈"]
    assert summaries[0].event.conversation_fingerprints == [conversation]


def test_balanced_candidate_content_fits_limit_across_sources(tmp_path: Path) -> None:
    input_limit_tokens = 1400
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        model_input_batch_target_tokens=input_limit_tokens,
    )
    runner = _build_runner(tmp_path, config=config, analyzer=TwoStageAnalyzer())
    events = [
        CollectedSourceEvent(
            draft_id=f"d{index}",
            person_name=person,
            source_file=f"{person}.md",
            event=_event(
                event_id=f"e{index}",
                title=f"{person}方案",
                content=(f"{person}补充方案执行过程和最终结论。" * 100),
            ),
        )
        for index, person in enumerate(("张三", "李四"), start=1)
    ]

    fitted, warnings = runner._fit_collected_grouping_events_to_limit(
        "2026-06-29",
        events,
        [],
    )

    assert runner._estimate_collected_grouping_prompt_tokens(
        "2026-06-29", fitted, []
    ) <= input_limit_tokens
    assert all(item.event.content for item in fitted)
    assert all(
        len(item.event.content) < len(original.event.content)
        for item, original in zip(fitted, events, strict=True)
    )
    assert all(item.prompt_original_content_chars for item in fitted)
    assert any("balanced collected candidate content" in item for item in warnings)


def test_single_oversized_render_event_is_split_below_limit(tmp_path: Path) -> None:
    input_limit_tokens = 1400
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        model_input_batch_target_tokens=input_limit_tokens,
    )
    runner = _build_runner(tmp_path, config=config, analyzer=TwoStageAnalyzer())
    source = CollectedSourceEvent(
        draft_id="d1",
        person_name="张三",
        source_file="张三.md",
        event=_event(
            event_id="e1",
            title="超大事项",
            content=("完成一段处理并记录具体结论。" * 400),
            conversation_fingerprints=["sha256:" + "d" * 64],
        ),
    )

    shards = runner._split_collected_source_event_for_render(
        "2026-06-29",
        source,
        depth=0,
    )

    assert len(shards) > 1
    assert all(
        runner._estimate_collected_render_prompt_tokens(
            "2026-06-29", [item], [[item.draft_id]]
        ) <= input_limit_tokens
        for item in shards
    )
    assert all(item.event.source_event_ids == ["e1"] for item in shards)


def test_two_stage_merge_combines_same_conversation_across_workstreams(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    conversation = "sha256:" + "c" * 64
    _write_day_doc(
        inbox / "2026-06-29-张三.md",
        [
            _event(
                event_id="e1",
                title="价格方案确认",
                content="张三确认价格调整方案。",
                workstream_name="经营策略",
                evidence_fingerprints=["sha256:" + "a" * 64],
                conversation_fingerprints=[conversation],
            )
        ],
        tmp_path,
    )
    _write_day_doc(
        inbox / "2026-06-29-李四.md",
        [
            _event(
                event_id="e2",
                title="客服执行反馈",
                content="李四确认客服执行口径。",
                workstream_name="客服运营",
                evidence_fingerprints=["sha256:" + "b" * 64],
                conversation_fingerprints=[conversation],
            )
        ],
        tmp_path,
    )
    analyzer = TwoStageAnalyzer("all")

    result = _build_runner(tmp_path, analyzer=analyzer).run("2026-06-29")

    assert result.merged_event_count == 1
    assert len(analyzer.grouping_calls) == 1
    assert len(analyzer.merge_calls) == 1
    output = MarkdownEventStore(config=RuntimeConfig()).parse_day_document(
        Path(result.output_path or "").read_text(encoding="utf-8")
    )
    assert set(output.events[0].source_people) == {"张三", "李四"}
    assert output.events[0].conversation_fingerprints == [conversation]
    assert output.events[0].workstream_name == ""
    assert any(
        "Allowed collected merge across different named workstreams" in warning
        for warning in result.warning_messages
    )


def test_same_conversation_is_not_automatically_merged(tmp_path: Path) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    conversation = "sha256:" + "c" * 64
    for person, event_id, title in (
        ("张三", "e1", "价格方案"),
        ("李四", "e2", "设备维修"),
    ):
        _write_day_doc(
            inbox / f"2026-06-29-{person}.md",
            [
                _event(
                    event_id=event_id,
                    title=title,
                    content=f"{person}处理{title}。",
                    conversation_fingerprints=[conversation],
                )
            ],
            tmp_path,
        )
    analyzer = TwoStageAnalyzer()

    result = _build_runner(tmp_path, analyzer=analyzer).run("2026-06-29")

    assert result.merged_event_count == 2
    assert len(analyzer.grouping_calls) == 1
    assert analyzer.merge_calls == []


def test_workstream_conflict_without_thread_evidence_is_still_split(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    for index, (person, workstream) in enumerate(
        (("张三", "项目甲"), ("李四", "项目乙")),
        start=1,
    ):
        _write_day_doc(
            inbox / f"2026-06-29-{person}.md",
            [
                _event(
                    event_id=f"e{index}",
                    title="共同标题",
                    content=f"{person}处理事项。",
                    workstream_name=workstream,
                )
            ],
            tmp_path,
        )
    analyzer = TwoStageAnalyzer("all")

    result = _build_runner(tmp_path, analyzer=analyzer).run("2026-06-29")

    assert result.merged_event_count == 2
    assert any(
        "different named workstreams cannot merge" in warning
        for warning in result.warning_messages
    )


def test_merge_collected_stops_before_analyzer_for_missing_conversation_evidence(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    _write_day_doc(
        inbox / "2026-06-29-旧文件.md",
        [
            _event(
                event_id="legacy",
                title="旧事件",
                content="旧事件没有会话证据。",
                conversation_fingerprints=[],
            )
        ],
        tmp_path,
    )
    analyzer = TwoStageAnalyzer("all")

    result = _build_runner(tmp_path, analyzer=analyzer).run("2026-06-29")

    assert result.status == "failed"
    assert result.output_path is None
    assert analyzer.grouping_calls == []
    assert analyzer.merge_calls == []
    assert any(
        "2026-06-29-旧文件.md (1 events)" in warning
        for warning in result.warning_messages
    )


def test_cross_department_upstream_events_keep_conversation_evidence(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    conversation = "sha256:" + "d" * 64
    for department, person, event_id in (
        ("部门甲", "张三", "e1"),
        ("部门乙", "李四", "e2"),
    ):
        event = replace(
            _event(
                event_id=event_id,
                title="跨部门上线事项",
                content=f"{department}确认上线事项。",
                conversation_fingerprints=[conversation],
            ),
            source_people=[person],
            source_event_ids=[event_id],
        )
        _write_day_doc(
            inbox / f"2026-06-29-{department}-merged.md",
            [event],
            tmp_path,
        )
    analyzer = TwoStageAnalyzer("all")

    result = _build_runner(tmp_path, analyzer=analyzer).run("2026-06-29")

    assert result.merged_event_count == 1
    output = MarkdownEventStore(config=RuntimeConfig()).parse_day_document(
        Path(result.output_path or "").read_text(encoding="utf-8")
    )
    assert set(output.events[0].source_people) == {"张三", "李四"}
    assert output.events[0].conversation_fingerprints == [conversation]


def test_relation_priority_batches_preserve_same_conversation_candidates(
    tmp_path: Path,
) -> None:
    class ConciseAnalyzer(TwoStageAnalyzer):
        def merge_collected_events(self, target_date, events, deterministic_groups):
            self.merge_calls.append(
                {
                    "target_date": target_date,
                    "events": events,
                    "deterministic_groups": deterministic_groups,
                }
            )
            return CollectedMergeResult(
                groups=[
                    CollectedMergeGroup(
                        group_id=f"rendered-{index}",
                        draft_ids=list(group),
                        title="批量汇总事项",
                        content="多人确认同一会话中的批量汇总事项。",
                        object_hint="批量汇总事项",
                        retention_reason="decision_made",
                        retention_detail="多人提供了同一事项的不同执行视角和确认结论。",
                    )
                    for index, group in enumerate(deterministic_groups, start=1)
                ]
            )

    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    conversation = "sha256:" + "e" * 64
    for index in range(12):
        _write_day_doc(
            inbox / f"2026-06-29-人员{index:02d}.md",
            [
                _event(
                    event_id=f"e{index}",
                    title=f"批量事项{index}",
                    content=(f"人员{index}补充批量事项。" * 80),
                    object_hint="批量事项",
                    conversation_fingerprints=[conversation],
                )
            ],
            tmp_path,
        )
    analyzer = ConciseAnalyzer("all")
    trace_root = tmp_path / "trace"

    result = _build_runner(
        tmp_path,
        analyzer=analyzer,
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            model_input_batch_target_tokens=2000,
            collected_merge_trace_enabled=True,
            collected_merge_trace_root=trace_root,
        ),
    ).run("2026-06-29")

    assert result.merged_event_count == 6
    assert any(
        "Skipped cross-batch coordination" in warning
        for warning in result.warning_messages
    )
    assert len(analyzer.grouping_calls) > 1
    assert len(analyzer.merge_calls) > 1
    summary = json.loads(
        (trace_root / "2026-06-29" / "summary.json").read_text(encoding="utf-8")
    )
    assert max(step["input_estimated_tokens"] for step in summary["steps"]) <= 2000
    assert {step["input_target_tokens"] for step in summary["steps"]} == {2000}
    output = MarkdownEventStore(config=RuntimeConfig()).parse_day_document(
        Path(result.output_path or "").read_text(encoding="utf-8")
    )
    assert output.events[0].conversation_fingerprints == [conversation]


def test_relation_priority_batches_match_july_14_event_scale(tmp_path: Path) -> None:
    class ConversationGroupingAnalyzer(TwoStageAnalyzer):
        def group_collected_events(self, target_date, events, deterministic_groups):
            self.grouping_calls.append(
                {
                    "target_date": target_date,
                    "events": events,
                    "deterministic_groups": deterministic_groups,
                }
            )
            grouped: dict[str, list[str]] = {}
            event_by_id = {item.draft_id: item for item in events}
            for item in events:
                conversation = item.event.conversation_fingerprints[0]
                grouped.setdefault(conversation, []).append(item.draft_id)
            return CollectedGroupingResult(
                groups=[
                    CollectedGroupingGroup(
                        group_id=f"candidate-{index}",
                        draft_ids=draft_ids,
                        summary_title=event_by_id[draft_ids[0]].event.title,
                        summary_content="多人围绕同一事项补充了执行信息。",
                        summary_object_hint=(
                            event_by_id[draft_ids[0]].event.object_hint
                        ),
                    )
                    for index, draft_ids in enumerate(grouped.values(), start=1)
                ]
            )

        def merge_collected_events(self, target_date, events, deterministic_groups):
            self.merge_calls.append(
                {
                    "target_date": target_date,
                    "events": events,
                    "deterministic_groups": deterministic_groups,
                }
            )
            event_by_id = {item.draft_id: item for item in events}
            return CollectedMergeResult(
                groups=[
                    CollectedMergeGroup(
                        group_id=f"rendered-{index}",
                        draft_ids=list(group),
                        title=event_by_id[group[0]].event.title,
                        content="多人确认同一事项的执行信息。",
                        object_hint=event_by_id[group[0]].event.object_hint,
                        retention_reason="decision_made",
                        retention_detail="多人围绕同一事项形成了明确、可追溯的执行结论。",
                    )
                    for index, group in enumerate(deterministic_groups, start=1)
                ]
            )

    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    source_events: list[list[WorkEvent]] = [[] for _ in range(6)]
    for index in range(195):
        conversation_index = index // 5
        source_events[index % len(source_events)].append(
            _event(
                event_id=f"event-{index:03d}",
                title=f"事项-{conversation_index:02d}",
                content=f"来源记录 {index:03d} 补充事项执行信息。",
                object_hint=f"事项-{conversation_index:02d}",
                conversation_fingerprints=[
                    "sha256:" + f"{conversation_index:064x}"
                ],
            )
        )
    for person_index, events in enumerate(source_events, start=1):
        _write_day_doc(
            inbox / f"2026-06-29-人员{person_index}.md",
            events,
            tmp_path,
        )

    analyzer = ConversationGroupingAnalyzer()
    trace_root = tmp_path / "trace"
    input_limit_tokens = 6200
    result = _build_runner(
        tmp_path,
        analyzer=analyzer,
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            model_input_batch_target_tokens=input_limit_tokens,
            collected_merge_trace_enabled=True,
            collected_merge_trace_root=trace_root,
        ),
    ).run("2026-06-29")

    assert result.source_file_count == 6
    assert result.source_event_count == 195
    assert result.merged_event_count == 39
    summary = json.loads(
        (trace_root / "2026-06-29" / "summary.json").read_text(encoding="utf-8")
    )
    assert (
        max(step["input_estimated_tokens"] for step in summary["steps"])
        <= input_limit_tokens
    )
    assert {step["input_target_tokens"] for step in summary["steps"]} == {
        input_limit_tokens
    }
    stages = {step["stage"] for step in summary["steps"]}
    assert any(stage.startswith("candidate_grouping_batch_") for stage in stages)
    assert not any(stage.startswith("candidate_reconciliation") for stage in stages)
    assert any(
        "Skipped cross-batch coordination" in warning
        for warning in summary["warning_messages"]
    )
    assert "content_merge" in stages
    output = MarkdownEventStore(config=RuntimeConfig()).parse_day_document(
        Path(result.output_path or "").read_text(encoding="utf-8")
    )
    assert len(
        {
            source_event_id
            for event in output.events
            for source_event_id in event.source_event_ids
        }
    ) == 195
    assert len(
        {
            fingerprint
            for event in output.events
            for fingerprint in event.conversation_fingerprints
        }
    ) == 39


def test_relation_priority_batching_fits_138_and_195_event_scales(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        model_input_batch_target_tokens=6200,
    )
    runner = _build_runner(tmp_path, config=config, analyzer=TwoStageAnalyzer())

    for event_count in (138, 195):
        events = [
            CollectedSourceEvent(
                draft_id=f"d-{event_count}-{index}",
                person_name=f"人员{index % 6}",
                source_file=f"人员{index % 6}.md",
                event=_event(
                    event_id=f"e-{event_count}-{index}",
                    title=f"事项{index // 5}",
                    content=f"来源记录{index}补充事项执行过程和结果。",
                    conversation_fingerprints=[
                        "sha256:" + f"{index // 5:064x}"
                    ],
                ),
            )
            for index in range(event_count)
        ]
        batches = runner._pack_collected_grouping_batches(
            "2026-06-29",
            events,
            [],
        )

        assert len(batches) > 1
        for batch in batches:
            fitted, _ = runner._fit_collected_grouping_events_to_limit(
                "2026-06-29",
                batch,
                [],
            )
            assert runner._estimate_collected_grouping_prompt_tokens(
                "2026-06-29",
                fitted,
                [],
            ) <= 6200


class ReviewAnalyzer(TwoStageAnalyzer):
    def __init__(self, *, split: bool = False, invalid: bool = False) -> None:
        super().__init__(groups="all")
        self.split = split
        self.invalid = invalid
        self.review_calls: list[dict[str, object]] = []

    def review_collected_group(
        self,
        target_date,
        events,
        candidate_group,
        *,
        review_reasons=None,
    ):
        self.review_calls.append(
            {
                "target_date": target_date,
                "events": list(events),
                "candidate_group": candidate_group,
                "review_reasons": list(review_reasons or []),
            }
        )
        draft_ids = [item.draft_id for item in events]
        if self.invalid:
            draft_ids = draft_ids[:1]
        groups = [[draft_id] for draft_id in draft_ids] if self.split else [draft_ids]
        return CollectedGroupingResult(
            split_reason=("业务对象和主要动作不同。" if self.split else ""),
            groups=[
                CollectedGroupingGroup(
                    group_id=f"review-{index}",
                    draft_ids=list(group),
                    summary_title="复核事项" if len(group) > 1 else "",
                    summary_content="复核后确认属于同一事项。" if len(group) > 1 else "",
                    summary_object_hint="复核事项" if len(group) > 1 else "",
                    group_reason=["same_object"] if len(group) > 1 else [],
                    risk_flags=[],
                )
                for index, group in enumerate(groups, start=1)
            ]
        )


def test_collected_merge_allows_personal_and_upstream_markdown_together(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    personal = _event(
        event_id="evt-shared",
        title="单人部门事项",
        content="张三确认单人部门事项。",
    )
    upstream = replace(
        personal,
        source_people=["张三"],
        source_event_ids=["evt-shared"],
    )
    _write_day_doc(inbox / "2026-06-29-张三.md", [personal], tmp_path)
    _write_day_doc(inbox / "2026-06-29-张三-merged.md", [upstream], tmp_path)
    analyzer = FakeAnalyzer()

    result = _build_runner(tmp_path, analyzer=analyzer).run("2026-06-29")

    assert result.output_path is not None
    assert result.source_file_count == 2
    assert result.source_event_count == 2
    assert len(analyzer.calls[0]["events"]) == 2
    assert not any("duplicate" in warning.casefold() for warning in result.warning_messages)


def test_one_person_department_can_keep_same_event_count(tmp_path: Path) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    _write_day_doc(
        inbox / "2026-06-29-张三.md",
        [_event(event_id="evt-one", title="单人事项", content="张三完成单人事项。")],
        tmp_path,
    )
    analyzer = TwoStageAnalyzer()

    result = _build_runner(tmp_path, analyzer=analyzer).run("2026-06-29")

    assert result.source_event_count == 1
    assert result.merged_event_count == 1
    assert result.quality_summary.event_count_output_input_ratio == 1.0
    assert analyzer.merge_calls == []


def test_source_report_owner_accumulates_across_merge_levels(tmp_path: Path) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    upstream = replace(
        _event(
            event_id="evt-upstream",
            title="跨级事项",
            content="上一级已经汇总跨级事项。",
        ),
        source_people=["张三"],
        source_event_ids=["evt-personal"],
        source_report_owners=["更早负责人"],
    )
    _write_day_doc(
        inbox / "2026-06-29-部门负责人-merged.md",
        [upstream],
        tmp_path,
    )

    result = _build_runner(tmp_path).run("2026-06-29")
    loaded = MarkdownEventStore(config=RuntimeConfig()).parse_day_document(
        Path(result.output_path or "").read_text(encoding="utf-8")
    )

    assert loaded.events[0].source_people == ["张三"]
    assert loaded.events[0].source_event_ids == ["evt-personal"]
    assert loaded.events[0].source_report_owners == ["更早负责人", "部门负责人"]


def test_high_risk_review_event_threshold_is_inclusive(tmp_path: Path) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        high_risk_source_event_count=10,
        high_risk_source_file_count=99,
    )
    nine_analyzer = ReviewAnalyzer()
    _write_day_doc(
        inbox / "2026-06-29-张三.md",
        [
            _event(event_id=f"evt-{index}", title="共同事项", content=f"事实 {index}")
            for index in range(9)
        ],
        tmp_path,
    )

    _build_runner(tmp_path, analyzer=nine_analyzer, config=config).run("2026-06-29")

    assert nine_analyzer.review_calls == []

    ten_analyzer = ReviewAnalyzer()
    _write_day_doc(
        inbox / "2026-06-29-张三.md",
        [
            _event(event_id=f"evt-{index}", title="共同事项", content=f"事实 {index}")
            for index in range(10)
        ],
        tmp_path,
    )

    result = _build_runner(tmp_path, analyzer=ten_analyzer, config=config).run(
        "2026-06-29"
    )

    assert len(ten_analyzer.review_calls) == 1
    assert result.quality_summary.high_risk_group_count == 1
    assert result.quality_summary.reviewed_group_count == 1


def test_high_risk_review_triggers_at_four_source_files(tmp_path: Path) -> None:
    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    for index in range(4):
        _write_day_doc(
            inbox / f"2026-06-29-人员{index}.md",
            [_event(event_id=f"evt-{index}", title="共同事项", content=f"事实 {index}")],
            tmp_path,
        )
    analyzer = ReviewAnalyzer()

    result = _build_runner(
        tmp_path,
        analyzer=analyzer,
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            high_risk_source_event_count=99,
            high_risk_source_file_count=4,
        ),
    ).run("2026-06-29")

    assert len(analyzer.review_calls) == 1
    assert result.quality_summary.review_required is True
    review_payload = json.loads(
        build_collected_review_prompt(
            "2026-06-29",
            analyzer.review_calls[0]["events"],
            analyzer.review_calls[0]["candidate_group"],
            config=RuntimeConfig(
                high_risk_source_event_count=99,
                high_risk_source_file_count=4,
            ),
        )
    )
    assert review_payload["review_reasons"] == ["source_file_count"]


def test_high_risk_review_reasons_follow_configured_conditions(tmp_path: Path) -> None:
    runner = _build_runner(tmp_path, analyzer=ReviewAnalyzer())
    same_workstream = [
        CollectedSourceEvent("d1", "张三", "a.md", _event(
            event_id="e1", title="事项", content="事实", workstream_name="项目A"
        )),
        CollectedSourceEvent("d2", "李四", "b.md", _event(
            event_id="e2", title="事项", content="事实", workstream_name="项目A"
        )),
    ]
    conflict_workstream = [
        same_workstream[0],
        replace(
            same_workstream[1],
            event=replace(same_workstream[1].event, workstream_name="项目B"),
        ),
    ]

    assert runner._collected_group_review_reasons(
        CollectedGroupingGroup("g1", ["d1", "d2"], risk_flags=["cross_batch"]),
        same_workstream,
    ) == ["cross_batch"]
    assert runner._collected_group_review_reasons(
        CollectedGroupingGroup("g2", ["d1", "d2"], was_repaired=True),
        same_workstream,
    ) == ["repaired_group"]
    assert runner._collected_group_review_reasons(
        CollectedGroupingGroup("g3", ["d1", "d2"]),
        conflict_workstream,
    ) == ["workstream_conflict"]
    assert runner._collected_group_review_reasons(
        CollectedGroupingGroup(
            "g4",
            ["d1", "d2"],
            risk_flags=["broad_object", "large_group"],
        ),
        same_workstream,
    ) == []
    cross_batch_disabled = _build_runner(
        tmp_path,
        analyzer=ReviewAnalyzer(),
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            review_cross_batch_groups=False,
            review_repaired_groups=True,
        ),
    )
    assert cross_batch_disabled._collected_group_review_reasons(
        CollectedGroupingGroup(
            "g5",
            ["d1", "d2"],
            summary_title="跨批事项",
            summary_content="跨批摘要完整。",
            summary_object_hint="跨批事项",
            risk_flags=["cross_batch"],
            was_repaired=False,
        ),
        same_workstream,
    ) == []


def test_high_risk_review_marks_disconnected_same_conversation_groups(
    tmp_path: Path,
) -> None:
    events = [
        CollectedSourceEvent(
            "d1",
            "张三",
            "a.md",
            _event(
                event_id="e1",
                title="技术交流会议安排",
                content="安排技术交流会议。",
                conversation_fingerprints=["conversation-1"],
                evidence_fingerprints=["message-1"],
            ),
        ),
        CollectedSourceEvent(
            "d2",
            "李四",
            "b.md",
            _event(
                event_id="e2",
                title="技术交流参会确认",
                content="确认参加技术交流会议。",
                conversation_fingerprints=["conversation-1"],
                evidence_fingerprints=["message-1"],
            ),
        ),
        CollectedSourceEvent(
            "d3",
            "张三",
            "a.md",
            _event(
                event_id="e3",
                title="奖励表转交",
                content="转交奖励表。",
                conversation_fingerprints=["conversation-1"],
                evidence_fingerprints=["message-2"],
                file_keys=["file-1"],
            ),
        ),
        CollectedSourceEvent(
            "d4",
            "李四",
            "b.md",
            _event(
                event_id="e4",
                title="奖励表接收",
                content="确认收到奖励表。",
                conversation_fingerprints=["conversation-1"],
                evidence_fingerprints=["message-2"],
                file_keys=["file-1"],
            ),
        ),
    ]
    group = CollectedGroupingGroup("g1", ["d1", "d2", "d3", "d4"])
    runner = _build_runner(tmp_path, analyzer=ReviewAnalyzer())

    assert runner._collected_group_review_reasons(group, events) == [
        "same_conversation_only"
    ]

    disabled_runner = _build_runner(
        tmp_path,
        analyzer=ReviewAnalyzer(),
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            review_same_conversation_only_groups=False,
        ),
    )
    assert disabled_runner._collected_group_review_reasons(group, events) == []


def test_same_conversation_only_review_retries_unsupported_subgroup(
    tmp_path: Path,
) -> None:
    class UnsupportedThenValidAnalyzer(ReviewAnalyzer):
        def review_collected_group(self, target_date, events, candidate_group, *, review_reasons=None):
            self.review_calls.append(
                {
                    "candidate_group": candidate_group,
                    "review_reasons": list(review_reasons or []),
                }
            )
            if len(self.review_calls) == 1:
                return CollectedGroupingResult(
                    groups=[
                        CollectedGroupingGroup(
                            "unsupported",
                            [item.draft_id for item in events],
                            summary_title="不同事项",
                            summary_content="仅因同一会话被放在一起。",
                            summary_object_hint="不同事项",
                            group_reason=["same_conversation"],
                        )
                    ]
                )
            return CollectedGroupingResult(
                groups=[
                    CollectedGroupingGroup(
                        "meeting",
                        [events[0].draft_id, events[1].draft_id],
                        summary_title="技术交流会议",
                        summary_content="安排技术交流会议并确认参会。",
                        summary_object_hint="技术交流会议",
                        split_reason="奖励表转交与技术交流会议是不同事项。",
                        group_reason=["shared_message"],
                    ),
                    CollectedGroupingGroup(
                        "rewards",
                        [events[2].draft_id, events[3].draft_id],
                        summary_title="奖励表转交",
                        summary_content="转交奖励表并确认接收。",
                        summary_object_hint="奖励表",
                        split_reason="奖励表转交与技术交流会议是不同事项。",
                        group_reason=["shared_file"],
                    ),
                ]
            )

    events = [
        CollectedSourceEvent(
            "d1",
            "张三",
            "a.md",
            _event(
                event_id="e1",
                title="技术交流会议安排",
                content="安排技术交流会议。",
                conversation_fingerprints=["conversation-1"],
                evidence_fingerprints=["message-1"],
            ),
        ),
        CollectedSourceEvent(
            "d2",
            "李四",
            "b.md",
            _event(
                event_id="e2",
                title="技术交流参会确认",
                content="确认参加技术交流会议。",
                conversation_fingerprints=["conversation-1"],
                evidence_fingerprints=["message-1"],
            ),
        ),
        CollectedSourceEvent(
            "d3",
            "张三",
            "a.md",
            _event(
                event_id="e3",
                title="奖励表转交",
                content="转交奖励表。",
                conversation_fingerprints=["conversation-1"],
                evidence_fingerprints=["message-2"],
                file_keys=["file-1"],
            ),
        ),
        CollectedSourceEvent(
            "d4",
            "李四",
            "b.md",
            _event(
                event_id="e4",
                title="奖励表接收",
                content="确认收到奖励表。",
                conversation_fingerprints=["conversation-1"],
                evidence_fingerprints=["message-2"],
                file_keys=["file-1"],
            ),
        ),
    ]
    analyzer = UnsupportedThenValidAnalyzer()
    runner = _build_runner(tmp_path, analyzer=analyzer)

    reviewed, warnings = runner._invoke_collected_review_with_retry(
        "2026-06-29",
        events,
        CollectedGroupingGroup("g1", [item.draft_id for item in events]),
        reasons=["same_conversation_only"],
    )

    assert [group.draft_ids for group in reviewed.groups] == [
        ["d1", "d2"],
        ["d3", "d4"],
    ]
    assert len(analyzer.review_calls) == 2
    assert any("unsupported merge basis" in warning for warning in warnings)


def test_same_conversation_review_accepts_same_deliverable_batch_reason(
    tmp_path: Path,
) -> None:
    class SharedFileThenDeliverableBatchAnalyzer(ReviewAnalyzer):
        def review_collected_group(
            self,
            target_date,
            events,
            candidate_group,
            *,
            review_reasons=None,
        ):
            self.review_calls.append({"candidate_group": candidate_group})
            reason = (
                "shared_file"
                if len(self.review_calls) == 1
                else "same_deliverable_batch"
            )
            return CollectedGroupingResult(
                groups=[
                    CollectedGroupingGroup(
                        "reports",
                        [item.draft_id for item in events],
                        summary_title="同批工作记录提交",
                        summary_content="两人分别提交了同一批次的工作记录。",
                        summary_object_hint="同批工作记录",
                        group_reason=[reason],
                    )
                ]
            )

    events = [
        CollectedSourceEvent(
            "d1",
            "张三",
            "张三.md",
            _event(
                event_id="e1",
                title="张三工作记录",
                content="提交张三工作记录。",
                conversation_fingerprints=["conversation-1"],
                file_keys=["file-1"],
            ),
        ),
        CollectedSourceEvent(
            "d2",
            "李四",
            "李四.md",
            _event(
                event_id="e2",
                title="李四工作记录",
                content="提交李四工作记录。",
                conversation_fingerprints=["conversation-1"],
                file_keys=["file-2"],
            ),
        ),
    ]
    analyzer = SharedFileThenDeliverableBatchAnalyzer()
    runner = _build_runner(tmp_path, analyzer=analyzer)

    reviewed, warnings = runner._invoke_collected_review_with_retry(
        "2026-06-29",
        events,
        CollectedGroupingGroup("g1", ["d1", "d2"]),
        reasons=["same_conversation_only"],
    )

    assert reviewed.groups[0].group_reason == ["same_deliverable_batch"]
    assert len(analyzer.review_calls) == 2
    assert any("unsupported merge basis" in warning for warning in warnings)


def test_other_high_risk_review_rejects_false_shared_file(tmp_path: Path) -> None:
    class FalseSharedFileAnalyzer(ReviewAnalyzer):
        def review_collected_group(
            self,
            target_date,
            events,
            candidate_group,
            *,
            review_reasons=None,
        ):
            self.review_calls.append({"candidate_group": candidate_group})
            return CollectedGroupingResult(
                groups=[
                    CollectedGroupingGroup(
                        "reports",
                        [item.draft_id for item in events],
                        summary_title="同批工作记录",
                        summary_content="两人分别提交了工作记录。",
                        summary_object_hint="工作记录",
                        group_reason=["shared_file"],
                    )
                ]
            )

    events = [
        CollectedSourceEvent(
            f"d{index}",
            f"人员{index}",
            f"人员{index}.md",
            _event(
                event_id=f"e{index}",
                title="工作记录",
                content="提交工作记录。",
                file_keys=[f"file-{index}"],
            ),
        )
        for index in range(2)
    ]
    analyzer = FalseSharedFileAnalyzer()
    runner = _build_runner(
        tmp_path,
        analyzer=analyzer,
        config=RuntimeConfig(collected_merge_missing_field_retry_limit=0),
    )

    with pytest.raises(AnalyzerProtocolError, match="unsupported merge basis"):
        runner._invoke_collected_review_with_retry(
            "2026-06-29",
            events,
            CollectedGroupingGroup("g1", ["d0", "d1"]),
            reasons=["source_event_count"],
        )


def test_high_risk_review_can_split_group_without_losing_sources(
    tmp_path: Path,
) -> None:
    events = [
        CollectedSourceEvent(
            f"d{index}",
            f"人员{index}",
            f"人员{index}.md",
            _event(event_id=f"e{index}", title=f"事项{index}", content=f"事实{index}"),
        )
        for index in range(2)
    ]
    analyzer = ReviewAnalyzer(split=True)
    runner = _build_runner(tmp_path, analyzer=analyzer)

    reviewed, _ = runner._review_high_risk_groups(
        "2026-06-29",
        events,
        CollectedGroupingResult(
            groups=[
                CollectedGroupingGroup(
                    "g1",
                    ["d0", "d1"],
                    risk_flags=["cross_batch"],
                )
            ]
        ),
    )

    assert [group.draft_ids for group in reviewed.groups] == [["d0"], ["d1"]]
    assert runner._collected_quality_counters["review_split_group_count"] == 1


def test_high_risk_review_skips_singleton_groups(tmp_path: Path) -> None:
    event = CollectedSourceEvent(
        "d1",
        "张三",
        "张三.md",
        _event(event_id="e1", title="单条事项", content="单条事项事实。"),
    )
    analyzer = ReviewAnalyzer()
    runner = _build_runner(tmp_path, analyzer=analyzer)

    reviewed, warnings = runner._review_high_risk_groups(
        "2026-06-29",
        [event],
        CollectedGroupingResult(
            groups=[CollectedGroupingGroup("g1", ["d1"], risk_flags=["cross_batch"])]
        ),
    )

    assert reviewed.groups[0].draft_ids == ["d1"]
    assert analyzer.review_calls == []
    assert warnings == []
    assert runner._collected_quality_counters["high_risk_group_count"] == 0


def test_high_risk_review_rejects_split_without_reason(tmp_path: Path) -> None:
    class MissingReasonAnalyzer(ReviewAnalyzer):
        def review_collected_group(self, target_date, events, candidate_group, *, review_reasons=None):
            self.review_calls.append({"candidate_group": candidate_group})
            return CollectedGroupingResult(
                groups=[
                    CollectedGroupingGroup("split-1", [events[0].draft_id]),
                    CollectedGroupingGroup("split-2", [events[1].draft_id]),
                ]
            )

    events = [
        CollectedSourceEvent(
            f"d{index}",
            f"人员{index}",
            f"人员{index}.md",
            _event(event_id=f"e{index}", title="候选事项", content=f"事实{index}"),
        )
        for index in range(2)
    ]
    runner = _build_runner(tmp_path, analyzer=MissingReasonAnalyzer())

    reviewed, warnings = runner._review_high_risk_groups(
        "2026-06-29",
        events,
        CollectedGroupingResult(
            groups=[CollectedGroupingGroup("g1", ["d0", "d1"], risk_flags=["cross_batch"])]
        ),
    )

    assert [group.draft_ids for group in reviewed.groups] == [["d0", "d1"]]
    assert any("no overall split reason" in warning for warning in warnings)


def test_high_risk_review_accepts_one_legacy_group_split_reason(
    tmp_path: Path,
) -> None:
    class OneLegacyReasonAnalyzer(ReviewAnalyzer):
        def review_collected_group(
            self,
            target_date,
            events,
            candidate_group,
            *,
            review_reasons=None,
        ):
            return CollectedGroupingResult(
                groups=[
                    CollectedGroupingGroup(
                        "split-1",
                        [events[0].draft_id],
                        split_reason="两个子组的业务对象不同。",
                    ),
                    CollectedGroupingGroup("split-2", [events[1].draft_id]),
                ]
            )

    events = [
        CollectedSourceEvent(
            f"d{index}",
            f"人员{index}",
            f"人员{index}.md",
            _event(event_id=f"e{index}", title="候选事项", content=f"事实{index}"),
        )
        for index in range(2)
    ]
    runner = _build_runner(tmp_path, analyzer=OneLegacyReasonAnalyzer())

    reviewed, warnings = runner._review_high_risk_groups(
        "2026-06-29",
        events,
        CollectedGroupingResult(
            groups=[
                CollectedGroupingGroup("g1", ["d0", "d1"], risk_flags=["cross_batch"])
            ]
        ),
    )

    assert [group.draft_ids for group in reviewed.groups] == [["d0"], ["d1"]]
    assert warnings == []


def test_high_risk_review_runs_three_multi_groups_in_parallel(tmp_path: Path) -> None:
    from threading import Barrier

    class ParallelReviewAnalyzer(ReviewAnalyzer):
        def __init__(self) -> None:
            super().__init__()
            self.barrier = Barrier(3)

        def review_collected_group(self, target_date, events, candidate_group, *, review_reasons=None):
            self.barrier.wait(timeout=1)
            return CollectedGroupingResult(groups=[candidate_group])

    events = [
        CollectedSourceEvent(
            f"d{index}",
            f"人员{index}",
            f"人员{index}.md",
            _event(event_id=f"e{index}", title="候选事项", content=f"事实{index}"),
        )
        for index in range(6)
    ]
    groups = [
        CollectedGroupingGroup(
            f"g{index}",
            [f"d{index * 2}", f"d{index * 2 + 1}"],
            risk_flags=["cross_batch"],
        )
        for index in range(3)
    ]
    runner = _build_runner(
        tmp_path,
        analyzer=ParallelReviewAnalyzer(),
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            max_concurrent_collected_merge_review_requests=3,
        ),
    )

    reviewed, _ = runner._review_high_risk_groups(
        "2026-06-29", events, CollectedGroupingResult(groups=groups)
    )

    assert [group.group_id for group in reviewed.groups] == ["g0", "g1", "g2"]


def test_high_risk_review_invalid_partition_retries_then_fails(
    tmp_path: Path,
) -> None:
    events = [
        CollectedSourceEvent(
            f"d{index}",
            f"人员{index}",
            f"人员{index}.md",
            _event(event_id=f"e{index}", title="事项", content=f"事实{index}"),
        )
        for index in range(2)
    ]
    analyzer = ReviewAnalyzer(invalid=True)
    runner = _build_runner(
        tmp_path,
        analyzer=analyzer,
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            collected_merge_missing_field_retry_limit=1,
        ),
    )

    with pytest.raises(AnalyzerProtocolError, match="did not preserve source coverage"):
        runner._invoke_collected_review_with_retry(
            "2026-06-29",
            events,
            CollectedGroupingGroup("g1", ["d0", "d1"]),
            reasons=["cross_batch"],
        )

    assert len(analyzer.review_calls) == 2


def test_high_risk_review_batches_large_group_within_token_limit(
    tmp_path: Path,
) -> None:
    events = [
        CollectedSourceEvent(
            f"d{index}",
            f"人员{index % 6}",
            f"人员{index % 6}.md",
            _event(
                event_id=f"e{index}",
                title="大型复核事项",
                content=f"来源 {index}：" + "补充执行过程和结果。" * 60,
                conversation_fingerprints=["sha256:" + "a" * 64],
            ),
        )
        for index in range(40)
    ]
    trace_root = tmp_path / "trace"
    analyzer = ReviewAnalyzer()
    runner = _build_runner(
        tmp_path,
        analyzer=analyzer,
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            model_input_batch_target_tokens=6200,
            collected_merge_trace_enabled=True,
            collected_merge_trace_root=trace_root,
        ),
    )
    runner._start_collected_merge_trace("2026-06-29", tmp_path)

    reviewed, warnings = runner._review_collected_group_with_batching(
        "2026-06-29",
        events,
        CollectedGroupingGroup("large", [item.draft_id for item in events]),
        reasons=["source_event_count"],
        depth=0,
    )

    assert set(reviewed.groups[0].draft_ids) == {item.draft_id for item in events}
    assert len(analyzer.review_calls) > 1
    assert any("review batches" in item for item in warnings)
    assert max(
        step["input_estimated_tokens"]
        for step in runner._collected_merge_trace_steps
    ) <= 6200


def test_high_risk_review_summarizes_single_oversized_content_before_review(
    tmp_path: Path,
) -> None:
    class OversizedReviewAnalyzer(ReviewAnalyzer):
        def merge_collected_events(self, target_date, events, deterministic_groups):
            self.merge_calls.append(list(deterministic_groups))
            return CollectedMergeResult(
                groups=[
                    CollectedMergeGroup(
                        group_id=f"summary-{index}",
                        draft_ids=list(group),
                        title="超长事项摘要",
                        content="已完整归纳超长事项的过程、结果、风险和待办。",
                        object_hint="超长事项",
                        retention_reason="decision_made",
                        retention_detail="超长事项形成了完整、可追溯的归纳结果。",
                        covered_draft_ids=list(group),
                        fact_items=[
                            CollectedFactItem(
                                text="已保留本组全部来源的关键事实。",
                                source_draft_ids=list(group),
                            )
                        ],
                    )
                    for index, group in enumerate(deterministic_groups, start=1)
                ]
            )

    event = CollectedSourceEvent(
        "d1",
        "张三",
        "张三.md",
        _event(
            event_id="e1",
            title="超长事项",
            content="执行过程、结果、风险和待办。" * 5000,
        ),
    )
    trace_root = tmp_path / "trace"
    analyzer = OversizedReviewAnalyzer()
    runner = _build_runner(
        tmp_path,
        analyzer=analyzer,
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            model_input_batch_target_tokens=6200,
            collected_merge_trace_enabled=True,
            collected_merge_trace_root=trace_root,
        ),
    )
    runner._start_collected_merge_trace("2026-06-29", tmp_path)

    reviewed, warnings = runner._review_collected_group_with_batching(
        "2026-06-29",
        [event],
        CollectedGroupingGroup("oversized", ["d1"], risk_flags=["cross_batch"]),
        reasons=["cross_batch"],
        depth=0,
    )

    assert reviewed.groups[0].draft_ids == ["d1"]
    assert analyzer.merge_calls
    assert len(analyzer.review_calls) == 1
    assert any("hierarchical content summary" in item for item in warnings)
    assert max(
        step["input_estimated_tokens"]
        for step in runner._collected_merge_trace_steps
    ) <= 6200


def test_collected_content_coverage_retries_only_current_group(tmp_path: Path) -> None:
    class CoverageAnalyzer(ReviewAnalyzer):
        def __init__(self, *, always_invalid: bool = False) -> None:
            super().__init__()
            self.always_invalid = always_invalid

        def merge_collected_events(self, target_date, events, deterministic_groups):
            self.merge_calls.append(list(deterministic_groups))
            group = list(deterministic_groups[0])
            valid = len(self.merge_calls) > 1 and not self.always_invalid
            return CollectedMergeResult(
                groups=[
                    CollectedMergeGroup(
                        group_id="rendered",
                        draft_ids=group,
                        title="覆盖事项",
                        content="完整归纳两个来源的事实。",
                        object_hint="覆盖事项",
                        retention_reason="decision_made",
                        retention_detail="两个来源共同形成覆盖事项结论。",
                        covered_draft_ids=(group if valid else []),
                        fact_items=(
                            [
                                CollectedFactItem(
                                    text=f"保留 {draft_id} 的事实。",
                                    source_draft_ids=[draft_id],
                                )
                                for draft_id in group
                            ]
                            if valid
                            else []
                        ),
                    )
                ]
            )

    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    for index, person in enumerate(("张三", "李四"), start=1):
        _write_day_doc(
            inbox / f"2026-06-29-{person}.md",
            [_event(event_id=f"evt-{index}", title="覆盖事项", content=f"事实 {index}")],
            tmp_path,
        )
    analyzer = CoverageAnalyzer()

    result = _build_runner(
        tmp_path,
        analyzer=analyzer,
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            collected_merge_missing_field_retry_limit=1,
        ),
    ).run("2026-06-29")

    assert result.output_path is not None
    assert len(analyzer.merge_calls) == 2
    assert result.quality_summary.content_retry_count == 1
    assert any("source coverage was invalid" in item for item in result.warning_messages)


def test_collected_content_coverage_terminal_failure_does_not_write_output(
    tmp_path: Path,
) -> None:
    class InvalidCoverageAnalyzer(ReviewAnalyzer):
        def merge_collected_events(self, target_date, events, deterministic_groups):
            group = list(deterministic_groups[0])
            return CollectedMergeResult(
                groups=[
                    CollectedMergeGroup(
                        group_id="invalid",
                        draft_ids=group,
                        title="覆盖事项",
                        content="遗漏了来源。",
                        object_hint="覆盖事项",
                        retention_reason="decision_made",
                        retention_detail="内容覆盖不完整。",
                        covered_draft_ids=[],
                        fact_items=[],
                    )
                ]
            )

    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    for index, person in enumerate(("张三", "李四"), start=1):
        _write_day_doc(
            inbox / f"2026-06-29-{person}.md",
            [_event(event_id=f"evt-{index}", title="覆盖事项", content=f"事实 {index}")],
            tmp_path,
        )

    result = _build_runner(
        tmp_path,
        analyzer=InvalidCoverageAnalyzer(),
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            collected_merge_missing_field_retry_limit=1,
        ),
    ).run("2026-06-29")

    expected_output = inbox / "2026-06-29-管理者-merged.md"
    assert result.output_path is None
    assert not expected_output.exists()
    assert any("did not preserve source coverage" in item for item in result.warning_messages)


def test_collected_quality_summary_is_deterministic_and_trace_matches(
    tmp_path: Path,
) -> None:
    parsed = [
        CollectedSourceEvent(
            "d1",
            "张三",
            "张三.md",
            replace(_event(event_id="e1", title="事项", content="甲乙"), source_report_owners=["负责人甲"]),
        ),
        CollectedSourceEvent(
            "d2",
            "李四",
            "李四.md",
            _event(event_id="e2", title="事项", content="丙丁戊"),
            source_report_owner="负责人乙",
        ),
        CollectedSourceEvent(
            "d3",
            "王五",
            "王五.md",
            _event(event_id="e3", title="已过滤", content="己"),
        ),
    ]
    output_events = [
        replace(
            _event(event_id="out", title="事项", content="汇总内容"),
            source_event_ids=["e1", "e2"],
        )
    ]
    quality = build_collected_quality_summary(
        parsed,
        parsed[:2],
        output_events,
        counters={
            "high_risk_group_count": 1,
            "reviewed_group_count": 1,
            "review_split_group_count": 0,
            "content_retry_count": 1,
            "shortened_prompt_count": 2,
            "review_required": 1,
        },
    )

    assert quality.input_event_count == 3
    assert quality.filtered_event_count == 2
    assert quality.output_event_count == 1
    assert quality.multi_source_group_count == 1
    assert quality.singleton_group_count == 0
    assert quality.max_source_events_per_group == 2
    assert quality.input_content_chars == sum(len(item.event.content) for item in parsed)
    assert quality.output_content_chars == len(output_events[0].content)
    assert quality.event_count_output_input_ratio == round(1 / 3, 4)
    assert quality.content_chars_output_input_ratio == round(
        len(output_events[0].content) / sum(len(item.event.content) for item in parsed),
        4,
    )
    assert quality.source_event_coverage_ratio == 1.0
    assert quality.source_report_owner_count == 2
    assert aggregate_collected_quality_summaries([quality, quality]).output_event_count == 2

    inbox = tmp_path / "merge_inbox" / "2026" / "06" / "29"
    _write_day_doc(
        inbox / "2026-06-29-张三.md",
        [_event(event_id="evt-trace", title="追踪事项", content="追踪内容")],
        tmp_path,
    )
    trace_root = tmp_path / "trace"
    result = _build_runner(
        tmp_path,
        analyzer=TwoStageAnalyzer(),
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            collected_merge_trace_enabled=True,
            collected_merge_trace_root=trace_root,
        ),
    ).run("2026-06-29")
    trace_summary = json.loads(
        (trace_root / "2026-06-29" / "summary.json").read_text(encoding="utf-8")
    )
    trace_markdown = (trace_root / "2026-06-29" / "summary.md").read_text(
        encoding="utf-8"
    )

    assert trace_summary["quality_summary"] == result.quality_summary.to_dict()
    assert result.to_dict()["quality_summary"] == trace_summary["quality_summary"]
    assert "## Quality Summary" in trace_markdown
