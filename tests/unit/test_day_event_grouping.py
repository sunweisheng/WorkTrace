from __future__ import annotations

import pytest

from src.worktrace.errors import AnalyzerProtocolError
from src.worktrace.models import (
    AttachmentMeta,
    CrossConversationGroup,
    CrossConversationGroupResult,
    NormalizedMessage,
    SourceBackedEventDraft,
)
from src.worktrace.pipeline.day_event_grouping import (
    build_day_group_review_components,
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
                file_name="方案.docx",
                mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                file_size=100,
            )
            for attachment_id in attachment_ids or []
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


def test_local_review_cannot_split_an_existing_legal_group() -> None:
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
    split_result = CrossConversationGroupResult(
        groups=_singleton_groups(candidates)
    )

    with pytest.raises(AnalyzerProtocolError, match="must not split"):
        validate_day_group_review_result(split_result, component)


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
