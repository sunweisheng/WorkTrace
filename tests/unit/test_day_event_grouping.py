from __future__ import annotations

from dataclasses import replace

import pytest

from src.worktrace.errors import AnalyzerProtocolError
from src.worktrace.models import (
    AttachmentMeta,
    CrossConversationGroup,
    CrossConversationGroupResult,
    DayGroupDiscoveryCandidate,
    DayGroupDiscoveryCheck,
    DayGroupDiscoveryResult,
    DayGroupRelationResolution,
    DayGroupReviewResult,
    NormalizedMessage,
    SourceBackedEventDraft,
)
from src.worktrace.pipeline.day_event_grouping import (
    build_day_group_discovery_groups,
    build_day_group_review_components,
    build_day_group_review_typical_arguments,
    normalize_attachment_base_name,
    replace_reviewed_day_group_components,
    validate_day_group_review_result,
)


def _draft(
    draft_id: str,
    message_id: str,
    *,
    slice_id: str,
    attachment_ids: list[str] | None = None,
) -> SourceBackedEventDraft:
    return SourceBackedEventDraft(
        draft_id=draft_id,
        date="2026-07-22",
        topic=f"事项 {draft_id}",
        content=f"处理事项 {draft_id}。",
        source_message_ids=[message_id],
        source_conversation_id="oc_shared",
        source_slice_id=slice_id,
        confidence=0.9,
        object_hint=f"对象 {draft_id}",
        retention_reason="decision_made",
        retention_detail=f"形成事项 {draft_id} 的结论。",
        referenced_attachment_ids=attachment_ids or [],
    )


def _message(
    message_id: str,
    *,
    reply_to: str | None = None,
    attachment_ids: list[str] | None = None,
    file_names: list[str] | None = None,
    mime_type: str = "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
) -> NormalizedMessage:
    return NormalizedMessage(
        conversation_id="oc_shared",
        conversation_name="项目群",
        message_id=message_id,
        sender_open_id="ou_self",
        sender_name="本人",
        send_time="2026-07-22T10:00:00+08:00",
        message_type="text",
        text=message_id,
        reply_to_message_id=reply_to,
        quote_message_id=None,
        attachments=[
            AttachmentMeta(
                attachment_id=attachment_id,
                file_name=(file_names or ["方案.docx"] * len(attachment_ids or []))[index],
                mime_type=mime_type,
                file_size=100,
            )
            for index, attachment_id in enumerate(attachment_ids or [])
        ],
    )


def _singleton_groups(candidates: list[SourceBackedEventDraft]) -> list[CrossConversationGroup]:
    return [
        CrossConversationGroup(
            group_id=f"group-{index:03d}",
            draft_ids=[candidate.draft_id],
            primary_draft_id=candidate.draft_id,
            merge_reason="单条保留",
        )
        for index, candidate in enumerate(candidates, start=1)
    ]


def test_day_group_review_example_uses_minimal_relation_evidence() -> None:
    candidates = [
        replace(
            _draft(f"d{index}", f"m{index}-1", slice_id=f"slice-{index}"),
            source_message_ids=[f"m{index}-{item}" for item in range(1, 11)],
        )
        for index in range(1, 4)
    ]
    discovery = DayGroupDiscoveryResult(
        group_checks=[
            DayGroupDiscoveryCheck(
                group_id="group-001",
                related_group_ids=["group-002"],
                reason="第一组与第二组可能相关。",
            ),
            DayGroupDiscoveryCheck(
                group_id="group-002",
                related_group_ids=["group-003"],
                reason="第二组与第三组可能相关。",
            ),
            DayGroupDiscoveryCheck(
                group_id="group-003",
                related_group_ids=[],
                reason="未发现其他关系。",
            ),
        ]
    )
    components = build_day_group_review_components(
        _singleton_groups(candidates),
        candidates,
        [],
        discovery_result=discovery,
    )

    arguments = build_day_group_review_typical_arguments(components[0])

    assert next(iter(arguments)) == "relation_resolutions"
    assert arguments["singleton_draft_ids"] == ["d1", "d2", "d3"]
    assert arguments["relation_resolutions"] == [
        {
            "relation_id": "day-group-review-001-relation-001",
            "decision": "separate",
            "connected_draft_ids": ["d1", "d2"],
            "reason": "结构占位理由，不代表当前关系应当分开。",
            "evidence_message_ids": ["m1-1", "m2-1"],
        },
        {
            "relation_id": "day-group-review-001-relation-002",
            "decision": "separate",
            "connected_draft_ids": ["d2", "d3"],
            "reason": "结构占位理由，不代表当前关系应当分开。",
            "evidence_message_ids": ["m2-1", "m3-1"],
        },
    ]


@pytest.mark.parametrize(
    ("candidates", "messages", "expected_relation"),
    [
        (
            [_draft("d1", "m1", slice_id="slice-a"), _draft("d2", "m2", slice_id="slice-a")],
            [_message("m1"), _message("m2")],
            "same_source_slice",
        ),
        (
            [_draft("d1", "m1", slice_id="slice-a"), _draft("d2", "m1", slice_id="slice-b")],
            [_message("m1")],
            "shared_message",
        ),
        (
            [_draft("d1", "m1", slice_id="slice-a"), _draft("d2", "m2", slice_id="slice-b")],
            [_message("m1"), _message("m2", reply_to="m1")],
            "direct_reply_or_quote",
        ),
        (
            [
                _draft("d1", "m1", slice_id="slice-a", attachment_ids=["file-1"]),
                _draft("d2", "m2", slice_id="slice-b", attachment_ids=["file-1"]),
            ],
            [_message("m1", attachment_ids=["file-1"]), _message("m2", attachment_ids=["file-1"])],
            "shared_file",
        ),
    ],
)
def test_four_structural_relations_create_local_review_components(
    candidates: list[SourceBackedEventDraft],
    messages: list[NormalizedMessage],
    expected_relation: str,
) -> None:
    components = build_day_group_review_components(
        _singleton_groups(candidates),
        candidates,
        messages,
    )

    assert len(components) == 1
    assert expected_relation in components[0].relation_reasons[0]["relation_types"]


def test_same_conversation_alone_does_not_trigger_local_review() -> None:
    candidates = [
        _draft("d1", "m1", slice_id="slice-a"),
        _draft("d2", "m2", slice_id="slice-b"),
    ]

    assert build_day_group_review_components(
        _singleton_groups(candidates),
        candidates,
        [_message("m1"), _message("m2")],
    ) == []


def test_same_attachment_base_name_ignores_configured_version_suffix() -> None:
    candidates = [
        _draft("d1", "m1", slice_id="slice-a", attachment_ids=["file-1"]),
        _draft("d2", "m2", slice_id="slice-b", attachment_ids=["file-2"]),
    ]
    components = build_day_group_review_components(
        _singleton_groups(candidates),
        candidates,
        [
            _message(
                "m1",
                attachment_ids=["file-1"],
                file_names=["6月奖励表-V2.0.xlsx"],
            ),
            _message(
                "m2",
                attachment_ids=["file-2"],
                file_names=["6月奖励表-v3.xlsx"],
            ),
        ],
        attachment_version_suffix_patterns=[
            r"(?:[-_\s]*v\s*\d+(?:\.\d+)*)$",
        ],
        attachment_ignored_mime_type_prefixes=["image/"],
    )

    assert len(components) == 1
    reason = components[0].relation_reasons[0]
    assert reason["relation_types"] == ["same_attachment_base_name"]
    assert reason["shared_attachment_base_names"] == ["6月奖励表.xlsx"]
    assert components[0].relation_sources == ["same_attachment_base_name"]


def test_attachment_base_name_keeps_business_numbers_and_extension() -> None:
    patterns = [r"(?:[-_\s]*v\s*\d+(?:\.\d+)*)$"]

    assert normalize_attachment_base_name("6月奖励表-V2.0.xlsx", patterns) == (
        "6月奖励表.xlsx"
    )
    assert normalize_attachment_base_name("7月奖励表-V3.xlsx", patterns) == (
        "7月奖励表.xlsx"
    )
    assert normalize_attachment_base_name("6月奖励表-V2.0.pdf", patterns) == (
        "6月奖励表.pdf"
    )


def test_image_attachment_name_does_not_trigger_review() -> None:
    candidates = [
        _draft("d1", "m1", slice_id="slice-a", attachment_ids=["image-1"]),
        _draft("d2", "m2", slice_id="slice-b", attachment_ids=["image-2"]),
    ]

    assert build_day_group_review_components(
        _singleton_groups(candidates),
        candidates,
        [
            _message(
                "m1",
                attachment_ids=["image-1"],
                file_names=["现场-V2.png"],
                mime_type="image/png",
            ),
            _message(
                "m2",
                attachment_ids=["image-2"],
                file_names=["现场-V3.png"],
                mime_type="image/png",
            ),
        ],
        attachment_version_suffix_patterns=[r"(?:[-_\s]*v\s*\d+)$"],
        attachment_ignored_mime_type_prefixes=["image/"],
    ) == []


def test_discovery_groups_use_only_group_id_and_selected_title() -> None:
    candidates = [
        _draft("d1", "m1", slice_id="slice-a"),
        _draft("d2", "m2", slice_id="slice-b"),
        _draft("d3", "m3", slice_id="slice-c"),
    ]
    candidates[0] = replace(candidates[0], topic="")
    groups = [
        CrossConversationGroup(
            group_id="group-001",
            draft_ids=["d1", "d2"],
            primary_draft_id="d1",
            merge_reason="同一事项。",
        ),
        CrossConversationGroup(
            group_id="group-002",
            draft_ids=["d3"],
            primary_draft_id="d3",
            merge_reason="单条保留",
        ),
    ]

    discovery_groups = build_day_group_discovery_groups(groups, candidates)

    assert discovery_groups == [
        {"group_id": "group-001", "title": "事项 d2"},
        {"group_id": "group-002", "title": "事项 d3"},
    ]
    assert all(set(item) == {"group_id", "title"} for item in discovery_groups)


def test_discovery_group_title_covers_every_initial_member() -> None:
    candidates = [
        _draft("d1", "m1", slice_id="slice-a"),
        _draft("d2", "m2", slice_id="slice-b"),
        _draft("d3", "m3", slice_id="slice-c"),
    ]
    groups = [
        CrossConversationGroup(
            group_id="group-001",
            draft_ids=["d1", "d2", "d3"],
            primary_draft_id="d2",
        )
    ]

    discovery_groups = build_day_group_discovery_groups(groups, candidates)

    assert discovery_groups == [
        {
            "group_id": "group-001",
            "title": "事项 d2；事项 d1；事项 d3",
        }
    ]


def test_overlapping_discovery_candidates_form_one_review_component() -> None:
    candidates = [
        _draft(f"d{index}", f"m{index}", slice_id=f"slice-{index}")
        for index in range(1, 5)
    ]
    discovery = DayGroupDiscoveryResult(
        candidate_groups=[
            DayGroupDiscoveryCandidate(
                group_ids=["group-001", "group-002"],
                reason="标题显示为同一交付过程。",
            ),
            DayGroupDiscoveryCandidate(
                group_ids=["group-002", "group-003", "group-004"],
                reason="标题显示为同一事项的后续动作。",
            ),
        ],
        group_checks=[
            DayGroupDiscoveryCheck(
                group_id="group-001",
                related_group_ids=["group-002"],
                reason="标题显示为同一交付过程。",
            ),
            DayGroupDiscoveryCheck(
                group_id="group-002",
                related_group_ids=["group-001", "group-003", "group-004"],
                reason="标题显示为同一事项的后续动作。",
            ),
            DayGroupDiscoveryCheck("group-003", [], "没有新增关系。"),
            DayGroupDiscoveryCheck("group-004", [], "没有新增关系。"),
        ],
    )

    components = build_day_group_review_components(
        _singleton_groups(candidates),
        candidates,
        [_message(f"m{index}") for index in range(1, 5)],
        discovery_result=discovery,
    )

    assert len(components) == 1
    assert [group.group_id for group in components[0].groups] == [
        "group-001",
        "group-002",
        "group-003",
        "group-004",
    ]
    assert components[0].relation_sources == ["title_discovery"]
    assert [
        relation["group_ids"] for relation in components[0].relation_reasons
    ] == [
        ["group-001", "group-002"],
        ["group-002", "group-003"],
        ["group-002", "group-004"],
    ]


def test_separate_review_may_name_representatives_from_both_sides() -> None:
    candidates = [
        _draft("d1", "m1", slice_id="slice-a"),
        _draft("d2", "m2", slice_id="slice-b"),
    ]
    groups = _singleton_groups(candidates)
    component = build_day_group_review_components(
        groups,
        candidates,
        [_message("m1"), _message("m2")],
        discovery_result=DayGroupDiscoveryResult(
            candidate_groups=[
                DayGroupDiscoveryCandidate(
                    ["group-001", "group-002"],
                    "标题显示可能相关。",
                )
            ],
            group_checks=[
                DayGroupDiscoveryCheck(
                    "group-001",
                    ["group-002"],
                    "标题显示可能相关。",
                ),
                DayGroupDiscoveryCheck("group-002", [], "没有新增关系。"),
            ],
        ),
    )[0]
    result = DayGroupReviewResult(
        grouping_result=CrossConversationGroupResult(groups=groups),
        relation_resolutions=[
            DayGroupRelationResolution(
                relation_id=str(component.relation_reasons[0]["relation_id"]),
                decision="separate",
                connected_draft_ids=["d1", "d2"],
                reason="完整内容显示为两个独立事项。",
                evidence_message_ids=["m1", "m2"],
            )
        ],
    )

    validated = validate_day_group_review_result(result, component)

    assert validated.relation_resolutions[0].connected_draft_ids == ["d1", "d2"]


def test_local_review_can_split_and_recombine_an_existing_group() -> None:
    candidates = [
        _draft("d1", "m1", slice_id="slice-a"),
        _draft("d2", "m2", slice_id="slice-a"),
        _draft("d3", "m3", slice_id="slice-a"),
    ]
    original_groups = [
        CrossConversationGroup(
            group_id="group-001",
            draft_ids=["d1", "d2"],
            primary_draft_id="d1",
            merge_reason="同一事项的连续动作。",
            evidence_message_ids=["m1"],
        ),
        CrossConversationGroup(
            group_id="group-002",
            draft_ids=["d3"],
            primary_draft_id="d3",
            merge_reason="单条保留",
        ),
    ]
    component = build_day_group_review_components(
        original_groups,
        candidates,
        [_message("m1"), _message("m2"), _message("m3")],
    )[0]
    split_result = DayGroupReviewResult(
        grouping_result=CrossConversationGroupResult(
            groups=[
                CrossConversationGroup(
                    group_id="replacement-001",
                    draft_ids=["d1", "d3"],
                    primary_draft_id="d1",
                    merge_reason="同一事项的具体处理和归档。",
                    evidence_message_ids=["m1", "m3"],
                ),
                CrossConversationGroup(
                    group_id="replacement-002",
                    draft_ids=["d2"],
                    primary_draft_id="d2",
                    merge_reason="单条保留",
                ),
            ]
        ),
        relation_resolutions=[
            DayGroupRelationResolution(
                relation_id=str(relation["relation_id"]),
                decision=(
                    "merged"
                    if {
                        str(relation["left_draft_id"]),
                        str(relation["right_draft_id"]),
                    }
                    == {"d1", "d3"}
                    else "separate"
                ),
                connected_draft_ids=(
                    ["d1", "d3"]
                    if {
                        str(relation["left_draft_id"]),
                        str(relation["right_draft_id"]),
                    }
                    == {"d1", "d3"}
                    else []
                ),
                reason=(
                    "d1 和 d3 直接描述同一具体过程。"
                    if {
                        str(relation["left_draft_id"]),
                        str(relation["right_draft_id"]),
                    }
                    == {"d1", "d3"}
                    else "d2 是独立背景事项。"
                ),
                evidence_message_ids=[
                    f"m{str(relation['left_draft_id'])[-1]}",
                    f"m{str(relation['right_draft_id'])[-1]}",
                ],
            )
            for relation in component.relation_reasons
        ],
    )

    validated = validate_day_group_review_result(split_result, component)

    assert [group.draft_ids for group in validated.grouping_result.groups] == [
        ["d1", "d3"],
        ["d2"],
    ]


def test_local_review_replacement_uses_stable_python_group_ids() -> None:
    candidates = [
        _draft("d1", "m1", slice_id="slice-a"),
        _draft("d2", "m2", slice_id="slice-a"),
    ]
    original_groups = _singleton_groups(candidates)
    component = build_day_group_review_components(
        original_groups,
        candidates,
        [_message("m1"), _message("m2")],
    )[0]
    replacement = CrossConversationGroupResult(
        groups=[
            CrossConversationGroup(
                group_id="ignored-model-id",
                draft_ids=["d1", "d2"],
                primary_draft_id="d1",
                merge_reason="方案确认后形成执行反馈。",
                evidence_message_ids=["m1", "m2"],
            )
        ]
    )

    groups = replace_reviewed_day_group_components(
        original_groups,
        {component.component_id: replacement},
        [component],
        candidates,
    )

    assert [group.group_id for group in groups] == ["group-001"]
    assert groups[0].draft_ids == ["d1", "d2"]
