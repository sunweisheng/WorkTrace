from __future__ import annotations

from pathlib import Path

from src.worktrace.config import RuntimeConfig
from src.worktrace.models import NormalizedMessage
from src.worktrace.pipeline.anchors import group_anchor_units


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
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        slice_context_before=1,
        slice_context_after=1,
    )
    messages = [
        _message("om_1", "ou_other", "2026-06-23T09:00:00+08:00"),
        _message("om_2", "ou_self", "2026-06-23T09:01:00+08:00"),
        _message("om_3", "ou_other", "2026-06-23T09:02:00+08:00", reply_to="om_2"),
        _message("om_4", "ou_other", "2026-06-23T09:03:00+08:00"),
    ]

    anchor_units = group_anchor_units(messages, "ou_self", config)

    assert len(anchor_units) == 1
    assert anchor_units[0].anchor_message_ids == ["om_2"]
    assert [item.message_id for item in anchor_units[0].messages] == ["om_1", "om_2", "om_3"]
    assert anchor_units[0].reply_relation_ids == ["om_2"]


def test_group_anchor_units_splits_separated_self_messages(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        slice_context_before=0,
        slice_context_after=0,
    )
    messages = [
        _message("om_1", "ou_self", "2026-06-23T09:00:00+08:00"),
        _message("om_2", "ou_other", "2026-06-23T09:01:00+08:00"),
        _message("om_3", "ou_self", "2026-06-23T09:02:00+08:00"),
    ]

    anchor_units = group_anchor_units(messages, "ou_self", config)

    assert [item.anchor_message_ids for item in anchor_units] == [["om_1"], ["om_3"]]
