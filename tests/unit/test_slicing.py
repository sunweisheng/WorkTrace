from __future__ import annotations

from pathlib import Path

from src.worktrace.config import RuntimeConfig
from src.worktrace.models import NormalizedMessage
from src.worktrace.pipeline.slicing import build_conversation_slices


def _message(
    message_id: str,
    sender_open_id: str,
    send_time: str,
    *,
    reply_to: str | None = None,
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
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )


def test_slicing_merges_overlapping_anchor_windows(tmp_path: Path) -> None:
    config = RuntimeConfig(data_root=tmp_path / "data", slice_context_before=1, slice_context_after=1, slice_base_limit=10)
    messages = [
        _message("om_1", "ou_other", "2026-06-22T09:00:00+08:00"),
        _message("om_2", "ou_self", "2026-06-22T09:01:00+08:00"),
        _message("om_3", "ou_other", "2026-06-22T09:02:00+08:00"),
        _message("om_4", "ou_self", "2026-06-22T09:03:00+08:00"),
    ]

    slices = build_conversation_slices(messages, "ou_self", config)

    assert len(slices) == 1
    assert slices[0].anchor_message_ids == ["om_2", "om_4"]
