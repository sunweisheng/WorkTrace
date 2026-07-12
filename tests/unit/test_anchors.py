from __future__ import annotations

from pathlib import Path

from src.worktrace.models import MessageReaction, NormalizedMessage
from src.worktrace.pipeline.anchors import group_anchor_units
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
