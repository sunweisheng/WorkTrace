from __future__ import annotations

from ..models import NormalizedMessage


SYSTEM_EVENT_TYPES = {"system", "recall", "chat_info", "group_event"}
SYSTEM_TEXT_MARKERS = ("撤回", "加入群聊", "移出群聊", "修改群名", "系统消息")


def is_zero_risk_filtered_message(message: NormalizedMessage) -> bool:
    if message.is_system or message.message_type in SYSTEM_EVENT_TYPES:
        return True
    if not message.text.strip() and not message.links and not message.attachments:
        return True
    if any(marker in message.text for marker in SYSTEM_TEXT_MARKERS):
        return True
    return False


def filter_messages(messages: list[NormalizedMessage]) -> list[NormalizedMessage]:
    return [message for message in messages if not is_zero_risk_filtered_message(message)]
