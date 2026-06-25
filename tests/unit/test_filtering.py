from __future__ import annotations

from src.worktrace.models import NormalizedMessage
from src.worktrace.pipeline.filtering import filter_messages


def _message(message_id: str, *, text: str, is_system: bool = False, message_type: str = "text") -> NormalizedMessage:
    return NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id=message_id,
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:00:00+08:00",
        message_type=message_type,
        text=text,
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=is_system,
    )


def test_filtering_removes_zero_risk_messages() -> None:
    messages = [
        _message("om_1", text=""),
        _message("om_2", text="正常沟通"),
        _message("om_3", text="修改群名", is_system=True),
    ]

    filtered = filter_messages(messages)
    assert [item.message_id for item in filtered] == ["om_2"]
