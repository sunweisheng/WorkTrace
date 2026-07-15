from __future__ import annotations

from pathlib import Path

from src.worktrace.models import ConversationSlice, MessageReaction, NormalizedMessage
from src.worktrace.pipeline.anchors import group_anchor_units
from src.worktrace.pipeline.initial_windows import (
    append_private_window_external_relations,
    build_initial_anchor_windows,
)
from src.worktrace.pipeline.validation import normalize_source_message_ids
from src.worktrace.reaction_catalog import ReactionCatalog, ReactionFallback, ReactionMetadata


def _message(
    message_id: str,
    sender_open_id: str,
    send_time: str,
    *,
    reply_to: str | None = None,
    quote_to: str | None = None,
) -> NormalizedMessage:
    return NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id=message_id,
        sender_open_id=sender_open_id,
        sender_name=sender_open_id,
        send_time=send_time,
        message_type="text",
        text=message_id,
        reply_to_message_id=reply_to,
        quote_message_id=quote_to,
        links=[],
        attachments=[],
        is_system=False,
    )


def test_group_anchor_units_keeps_small_anchor_window(tmp_path: Path) -> None:
    messages = [
        _message("om_1", "ou_other", "2026-06-23T09:00:00+08:00"),
        _message("om_2", "ou_self", "2026-06-23T09:01:00+08:00"),
        _message("om_3", "ou_other", "2026-06-23T09:02:00+08:00", reply_to="om_2"),
        _message("om_4", "ou_other", "2026-06-23T09:03:00+08:00"),
    ]

    anchor_units = group_anchor_units(messages, "ou_self", before_limit=1, after_limit=1)

    assert len(anchor_units) == 1
    assert anchor_units[0].anchor_message_ids == ["om_2"]
    assert [item.message_id for item in anchor_units[0].messages] == ["om_1", "om_2", "om_3"]
    assert anchor_units[0].reply_relation_ids == ["om_2"]


def test_group_anchor_units_splits_separated_self_messages(tmp_path: Path) -> None:
    messages = [
        _message("om_1", "ou_self", "2026-06-23T09:00:00+08:00"),
        _message("om_2", "ou_other", "2026-06-23T09:01:00+08:00"),
        _message("om_3", "ou_self", "2026-06-23T09:02:00+08:00"),
    ]

    anchor_units = group_anchor_units(messages, "ou_self", before_limit=0, after_limit=0)

    assert [item.anchor_message_ids for item in anchor_units] == [["om_1"], ["om_3"]]


def test_group_anchor_units_creates_reaction_only_anchor_with_catalog_metadata() -> None:
    message = _message("om_1", "ou_other", "2026-06-23T09:00:00+08:00")
    messages = [
        NormalizedMessage(
            **(
                message.__dict__
                | {
                    "reactions": [
                        MessageReaction(
                            reaction_id="reaction-1",
                            operator_open_id="ou_self",
                            emoji_type="THUMBSUP",
                            action_time="2026-06-23T09:01:00+08:00",
                        )
                    ]
                }
            )
        )
    ]

    anchor_units = group_anchor_units(
        messages,
        "ou_self",
        before_limit=1,
        after_limit=1,
        reaction_catalog=ReactionCatalog(
            source_id="feishu",
            entries=(
                ReactionMetadata(
                    emoji_type="THUMBSUP",
                    name="点赞",
                    description="表示认可。",
                    semantic="affirmative",
                ),
            ),
            fallback=ReactionFallback("其他", "{emoji_type}", "other"),
        ),
    )

    assert len(anchor_units) == 1
    assert anchor_units[0].anchor_message_ids == ["om_1"]
    assert anchor_units[0].anchor_signals[0].kind == "reaction"
    assert anchor_units[0].anchor_signals[0].semantic == "affirmative"
    assert anchor_units[0].anchor_signals[0].emoji_name == "点赞"


def test_group_anchor_units_attaches_reaction_inside_text_window() -> None:
    reacted = _message("om_1", "ou_other", "2026-06-23T09:00:00+08:00")
    messages = [
        NormalizedMessage(
            **(
                reacted.__dict__
                | {
                    "reactions": [
                        MessageReaction(
                            reaction_id="reaction-1",
                            operator_open_id="ou_self",
                            emoji_type="THUMBSUP",
                            action_time="2026-06-23T09:01:00+08:00",
                        )
                    ]
                }
            )
        ),
        _message("om_2", "ou_self", "2026-06-23T09:02:00+08:00"),
    ]

    anchor_units = group_anchor_units(messages, "ou_self", before_limit=1, after_limit=1)

    assert len(anchor_units) == 1
    assert {item.kind for item in anchor_units[0].anchor_signals} == {"text", "reaction"}


def _initial_windows(messages: list[NormalizedMessage]):
    return build_initial_anchor_windows(
        messages,
        "ou_self",
        max_anchor_gap_minutes=10,
        max_unrelated_intervening_messages=3,
        initial_context_messages_before=2,
    )


def test_initial_windows_join_nearby_self_messages_and_split_after_ten_minutes() -> None:
    messages = [
        _message("om_1", "ou_self", "2026-06-23T09:00:00+08:00"),
        _message("om_2", "ou_other", "2026-06-23T09:02:00+08:00"),
        _message("om_3", "ou_self", "2026-06-23T09:10:00+08:00"),
        _message("om_4", "ou_other", "2026-06-23T09:11:00+08:00"),
        _message("om_5", "ou_self", "2026-06-23T09:21:00+08:00"),
    ]

    windows = _initial_windows(messages)

    assert [item.anchor_message_ids for item in windows] == [["om_1", "om_3"], ["om_5"]]
    assert windows[0].base_message_ids == ["om_1", "om_2", "om_3"]


def test_initial_windows_keep_reaction_only_anchor() -> None:
    message = _message("om_1", "ou_other", "2026-06-23T09:00:00+08:00")
    reacted = NormalizedMessage(
        **(
            message.__dict__
            | {
                "reactions": [
                    MessageReaction(
                        reaction_id="reaction-1",
                        operator_open_id="ou_self",
                        emoji_type="THUMBSUP",
                        action_time="2026-06-23T09:01:00+08:00",
                    )
                ]
            }
        )
    )

    windows = _initial_windows([reacted])

    assert windows[0].anchor_message_ids == ["om_1"]
    assert windows[0].anchor_signals[0].kind == "reaction"


def test_initial_windows_split_after_four_unrelated_messages_but_keep_mentions_and_replies() -> None:
    related = _message("om_2", "ou_other", "2026-06-23T09:01:00+08:00", reply_to="om_1")
    mentioned = NormalizedMessage(**(related.__dict__ | {"message_id": "om_3", "reply_to_message_id": None, "mentioned_open_ids": ["ou_self"]}))
    messages = [
        _message("om_1", "ou_self", "2026-06-23T09:00:00+08:00"),
        related,
        mentioned,
        _message("om_4", "ou_other", "2026-06-23T09:03:00+08:00"),
        _message("om_5", "ou_other", "2026-06-23T09:04:00+08:00"),
        _message("om_6", "ou_other", "2026-06-23T09:05:00+08:00"),
        _message("om_7", "ou_other", "2026-06-23T09:06:00+08:00"),
        _message("om_8", "ou_self", "2026-06-23T09:07:00+08:00"),
    ]

    windows = _initial_windows(messages)

    assert len(windows) == 2


def test_initial_windows_add_external_reply_context_and_allow_reuse() -> None:
    messages = [
        _message("om_1", "ou_self", "2026-06-23T09:00:00+08:00"),
        _message("om_2", "ou_other", "2026-06-23T09:01:00+08:00"),
        _message("om_3", "ou_self", "2026-06-23T09:20:00+08:00"),
        _message("om_4", "ou_other", "2026-06-23T09:21:00+08:00", quote_to="om_1"),
        _message("om_5", "ou_other", "2026-06-23T09:22:00+08:00", reply_to="om_3", quote_to="om_1"),
    ]

    windows = _initial_windows(messages)

    assert [item.relation_context_message_ids for item in windows] == [["om_4", "om_5"], ["om_5"]]
    assert "om_4" not in windows[0].base_message_ids
    first = windows[0]
    conversation_slice = ConversationSlice(
        slice_id="window-1",
        conversation_id=first.conversation_id,
        conversation_name=first.conversation_name,
        anchor_message_ids=first.anchor_message_ids,
        in_day_message_ids=first.base_message_ids,
        messages=first.messages,
        primary_message_ids=first.base_message_ids,
        context_message_ids=first.relation_context_message_ids,
    )
    assert normalize_source_message_ids(["om_4", "om_5"], conversation_slice) == []


def test_initial_windows_add_external_parent_referenced_by_main_message() -> None:
    messages = [
        _message("om_parent", "ou_other", "2026-06-23T08:40:00+08:00"),
        _message("om_1", "ou_self", "2026-06-23T09:00:00+08:00", reply_to="om_parent"),
    ]

    window = _initial_windows(messages)[0]

    assert window.base_message_ids == ["om_1"]
    assert window.relation_context_message_ids == ["om_parent"]


def test_initial_windows_add_two_preceding_messages_as_reusable_timeline_context() -> None:
    messages = [
        _message("om_1", "ou_other", "2026-06-23T08:57:00+08:00"),
        _message("om_2", "ou_other", "2026-06-23T08:58:00+08:00"),
        _message("om_3", "ou_self", "2026-06-23T09:00:00+08:00"),
        _message("om_4", "ou_other", "2026-06-23T09:20:00+08:00"),
        _message("om_5", "ou_self", "2026-06-23T09:21:00+08:00"),
    ]

    first, second = _initial_windows(messages)

    assert first.timeline_context_message_ids == ["om_1", "om_2"]
    assert second.timeline_context_message_ids == ["om_3", "om_4"]


def test_initial_windows_keep_private_conversation_as_one_window() -> None:
    first = _message("om_1", "ou_self", "2026-06-23T09:00:00+08:00")
    messages = [
        NormalizedMessage(**(first.__dict__ | {"conversation_mode": "p2p"})),
        _message("om_2", "ou_other", "2026-06-23T09:20:00+08:00"),
        _message("om_3", "ou_self", "2026-06-23T09:50:00+08:00"),
    ]

    windows = _initial_windows(messages)

    assert len(windows) == 1
    assert windows[0].base_message_ids == ["om_1", "om_2", "om_3"]


def test_private_window_adds_external_reply_and_parent_context() -> None:
    main = NormalizedMessage(
        **(
            _message("om_main", "ou_self", "2026-06-23T09:00:00+08:00").__dict__
            | {"conversation_mode": "p2p", "reply_to_message_id": "om_parent"}
        )
    )
    windows = _initial_windows([main])
    parent = _message("om_parent", "ou_other", "2026-06-22T20:00:00+08:00")
    reply = _message(
        "om_reply", "ou_other", "2026-06-24T09:00:00+08:00", reply_to="om_main"
    )

    class PrivateSource:
        def fetch_messages_by_ids(self, conversation_id, message_ids):
            if message_ids == ["om_main"]:
                return [main, reply]
            if message_ids == ["om_parent"]:
                return [parent]
            return []

    hydrated = append_private_window_external_relations(
        windows,
        chat_source=PrivateSource(),
    )

    assert [message.message_id for message in hydrated[0].messages] == [
        "om_parent",
        "om_main",
        "om_reply",
    ]
    assert hydrated[0].relation_context_message_ids == ["om_reply", "om_parent"]
