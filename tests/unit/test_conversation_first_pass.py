from __future__ import annotations

from pathlib import Path

from src.worktrace.config import RuntimeConfig
from src.worktrace.models import NormalizedMessage
from src.worktrace.pipeline.conversation_first_pass import build_conversation_level_slices


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


def test_conversation_first_pass_prefers_anchor_parent_and_nearby_messages(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(data_root=tmp_path / "data", slice_base_limit=4)
    messages = [
        _message("om_1", "ou_other", "2026-06-22T09:00:00+08:00"),
        _message("om_2", "ou_other", "2026-06-22T09:01:00+08:00"),
        _message("om_3", "ou_self", "2026-06-22T09:02:00+08:00", quote_to="om_1"),
        _message("om_4", "ou_other", "2026-06-22T09:03:00+08:00", reply_to="om_3"),
        _message("om_5", "ou_other", "2026-06-22T09:04:00+08:00"),
        _message("om_6", "ou_other", "2026-06-22T09:05:00+08:00"),
    ]

    slices = build_conversation_level_slices(messages, "ou_self", config)

    assert len(slices) == 1
    kept_ids = [message.message_id for message in slices[0].messages]
    assert kept_ids == ["om_1", "om_2", "om_3", "om_4"]


def test_conversation_first_pass_prefers_nearer_messages_when_relation_is_equal(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(data_root=tmp_path / "data", slice_base_limit=3)
    messages = [
        _message("om_1", "ou_other", "2026-06-22T09:00:00+08:00"),
        _message("om_2", "ou_other", "2026-06-22T09:01:00+08:00"),
        _message("om_3", "ou_self", "2026-06-22T09:02:00+08:00"),
        _message("om_4", "ou_other", "2026-06-22T09:03:00+08:00"),
        _message("om_5", "ou_other", "2026-06-22T09:04:00+08:00"),
    ]

    slices = build_conversation_level_slices(messages, "ou_self", config)

    kept_ids = [message.message_id for message in slices[0].messages]
    assert kept_ids == ["om_2", "om_3", "om_4"]
