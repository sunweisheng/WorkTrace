from __future__ import annotations

from src.worktrace.models import ConversationSlice, NormalizedMessage, SourceBackedEventDraft
from src.worktrace.pipeline.direct_relation_filter import (
    is_self_related_candidate_draft,
)


SELF_ASSIGNMENT_CUES = ("帮", "麻烦", "请", "需要你", "你来")
SELF_ASSIGNMENT_ACTIONS = (
    "处理",
    "确认",
    "推进",
    "反馈",
    "删除",
    "审批",
    "看下",
    "核对",
    "补充",
    "提个",
    "发一下",
)


def _message(
    message_id: str,
    *,
    sender_open_id: str,
    text: str,
    reply_to_message_id: str | None = None,
    quote_message_id: str | None = None,
) -> NormalizedMessage:
    return NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id=message_id,
        sender_open_id=sender_open_id,
        sender_name=sender_open_id,
        send_time=f"2026-07-08T10:0{message_id[-1]}:00+08:00",
        message_type="text",
        text=text,
        reply_to_message_id=reply_to_message_id,
        quote_message_id=quote_message_id,
        links=[],
        attachments=[],
        is_system=False,
    )


def _slice(messages: list[NormalizedMessage]) -> ConversationSlice:
    anchor_ids = [
        message.message_id
        for message in messages
        if message.sender_open_id == "ou_self"
    ]
    return ConversationSlice(
        slice_id="oc_1:om_1",
        conversation_id="oc_1",
        conversation_name="项目群",
        anchor_message_ids=anchor_ids,
        in_day_message_ids=[message.message_id for message in messages],
        messages=messages,
    )


def _draft(source_message_ids: list[str]) -> SourceBackedEventDraft:
    return SourceBackedEventDraft(
        draft_id="draft-1",
        date="2026-07-08",
        topic="聊天记录清理跟进",
        content="丁金龙要求删除与本人有关的聊天记录，付晨提出提看板处理。",
        source_message_ids=source_message_ids,
        source_conversation_id="oc_1",
        source_slice_id="oc_1:om_1",
        confidence=0.9,
        action_label="跟进",
        object_hint="聊天记录清理",
        retention_reason="follow_up_assigned",
        retention_detail="丁金龙明确要求删除记录，付晨提出后续看板方案。",
    )


def test_self_message_source_is_self_related() -> None:
    conversation_slice = _slice(
        [
            _message("om_1", sender_open_id="ou_self", text="推进发布"),
            _message("om_2", sender_open_id="ou_other", text="收到"),
        ]
    )

    assert is_self_related_candidate_draft(
        _draft(["om_1"]),
        conversation_slice,
        self_open_id="ou_self",
        self_display_name="张玉环",
        self_assignment_cues=SELF_ASSIGNMENT_CUES,
        self_assignment_actions=SELF_ASSIGNMENT_ACTIONS,
    )


def test_direct_reply_to_self_anchor_is_self_related() -> None:
    conversation_slice = _slice(
        [
            _message("om_1", sender_open_id="ou_self", text="推进发布"),
            _message(
                "om_2",
                sender_open_id="ou_other",
                text="收到，我来处理",
                reply_to_message_id="om_1",
            ),
        ]
    )

    assert is_self_related_candidate_draft(
        _draft(["om_2"]),
        conversation_slice,
        self_open_id="ou_self",
        self_display_name="张玉环",
        self_assignment_cues=SELF_ASSIGNMENT_CUES,
        self_assignment_actions=SELF_ASSIGNMENT_ACTIONS,
    )


def test_explicit_assignment_to_self_name_is_self_related() -> None:
    conversation_slice = _slice(
        [
            _message("om_1", sender_open_id="ou_self", text="我在"),
            _message(
                "om_2",
                sender_open_id="ou_other",
                text="@张玉环 麻烦你提个看板，让开发删除经销商聊天记录",
            ),
        ]
    )

    assert is_self_related_candidate_draft(
        _draft(["om_2"]),
        conversation_slice,
        self_open_id="ou_self",
        self_display_name="张玉环",
        self_assignment_cues=SELF_ASSIGNMENT_CUES,
        self_assignment_actions=SELF_ASSIGNMENT_ACTIONS,
    )


def test_explicit_assignment_requires_configured_actions() -> None:
    conversation_slice = _slice(
        [
            _message("om_1", sender_open_id="ou_self", text="我在"),
            _message(
                "om_2",
                sender_open_id="ou_other",
                text="@张玉环 麻烦你提个看板，让开发删除经销商聊天记录",
            ),
        ]
    )

    assert not is_self_related_candidate_draft(
        _draft(["om_2"]),
        conversation_slice,
        self_open_id="ou_self",
        self_display_name="张玉环",
        self_assignment_cues=(),
        self_assignment_actions=(),
    )


def test_other_people_discussion_is_not_self_related() -> None:
    conversation_slice = _slice(
        [
            _message("om_1", sender_open_id="ou_self", text="我知道了"),
            _message(
                "om_2",
                sender_open_id="ou_other_1",
                text="不需要，删除吧，把所有和我有关的都删除吧",
            ),
            _message(
                "om_3",
                sender_open_id="ou_other_2",
                text="那能找人帮您提个看板吧，我找开发把所有经销商的都删了",
            ),
        ]
    )

    assert not is_self_related_candidate_draft(
        _draft(["om_2", "om_3"]),
        conversation_slice,
        self_open_id="ou_self",
        self_display_name="张玉环",
        self_assignment_cues=SELF_ASSIGNMENT_CUES,
        self_assignment_actions=SELF_ASSIGNMENT_ACTIONS,
    )


def test_plain_name_mention_without_assignment_is_not_self_related() -> None:
    conversation_slice = _slice(
        [
            _message("om_1", sender_open_id="ou_self", text="我在"),
            _message(
                "om_2",
                sender_open_id="ou_other",
                text="张玉环之前也处理过类似问题，丁金龙这次让付晨处理",
            ),
        ]
    )

    assert not is_self_related_candidate_draft(
        _draft(["om_2"]),
        conversation_slice,
        self_open_id="ou_self",
        self_display_name="张玉环",
        self_assignment_cues=SELF_ASSIGNMENT_CUES,
        self_assignment_actions=SELF_ASSIGNMENT_ACTIONS,
    )
