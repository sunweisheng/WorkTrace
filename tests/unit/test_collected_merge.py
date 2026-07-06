from __future__ import annotations

import json
from pathlib import Path

from src.worktrace.collected_merge import (
    CollectedMergeRunner,
    extract_person_name_from_filename,
)
from src.worktrace.analyzers.prompts import build_collected_merge_prompt
from src.worktrace.config import RuntimeConfig
from src.worktrace.errors import DeliveryError
from src.worktrace.models import (
    CollectedMergeGroup,
    CollectedMergeOutput,
    CollectedMergeResult,
    CollectedMergeRunResult,
    CollectedSourceEvent,
    DayDocument,
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


def _event(
    *,
    event_id: str,
    title: str,
    content: str,
    object_hint: str | None = None,
    retention_reason: str = "decision_made",
    retention_detail: str | None = None,
) -> WorkEvent:
    return WorkEvent(
        date="2026-06-29",
        event_id=event_id,
        title=title,
        content=content,
        object_hint=object_hint or title,
        retention_reason=retention_reason,
        retention_detail=retention_detail or f"确认{title}的具体结果。",
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
    analyzer=None,
    delivery_channel=None,
    command_runner=None,
) -> CollectedMergeRunner:
    return CollectedMergeRunner(
        config=RuntimeConfig(data_root=tmp_path / "data"),
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
    assert "- 来源事件 ID: evt-shared" in content
    assert "项目排期确认" in content


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


def test_collected_merge_warns_when_merge_owner_source_is_missing(tmp_path: Path) -> None:
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

    assert any(
        "No merge-owner personal event markdown matched current user '管理者'" in warning
        for warning in result.warning_messages
    )


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
            confidential_event_keywords=("工资", "薪资", "薪酬"),
            non_work_sensitive_keywords=("吵架", "辱骂"),
        ),
    )

    payload = json.loads(prompt)

    assert payload["merge_owner_person"] == "张三"
    assert payload["remaining_events"][0]["is_merge_owner_source"] is True
    assert "涉及工资、薪资、薪酬" in prompt
    assert "涉及吵架、辱骂" in prompt
    assert "不要输出对应 group" in prompt
    assert "retention_reason" in prompt
    assert "以该来源事件为主" in prompt
    assert "最终 group 必须以 1.0.5 为主事实" in prompt


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


def test_collected_merge_filters_group_missing_retention_reason(tmp_path: Path) -> None:
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

    assert result.merged_event_count == 0
    content = (inbox / "2026-06-29-管理者-merged.md").read_text(encoding="utf-8")
    assert "_当天没有提炼出需要保留的工作事件。_" in content


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
                warning_messages=["warning"],
                self_delivery_status="success",
            )
        ],
    )

    payload = result.to_dict()
    restored = CollectedMergeRunResult.from_dict(payload)

    assert payload["outputs"][0]["source_event_count"] == 2
    assert restored.outputs[0].warning_messages == ["warning"]
