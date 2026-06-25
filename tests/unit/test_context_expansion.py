from __future__ import annotations

from pathlib import Path

from src.worktrace.config import RuntimeConfig
from src.worktrace.constants import ContextDirection, ContextRequestType
from src.worktrace.models import (
    AttachmentMeta,
    ContextRequest,
    ConversationSlice,
    NormalizedMessage,
)
from src.worktrace.pipeline.context_expansion import expand_slice_context
from src.worktrace.resolvers.feishu_message import FeishuMessageContentResolver


class FakeChatSource:
    def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
        return [
            NormalizedMessage(
                conversation_id=conversation_id,
                conversation_name="项目群",
                message_id="om_2",
                sender_open_id="ou_other",
                sender_name="Bob",
                send_time="2026-06-21T23:59:00+08:00",
                message_type="text",
                text="补前文",
                reply_to_message_id=None,
                quote_message_id=None,
                links=[],
                attachments=[],
                is_system=False,
            )
        ]


def test_expand_slice_context_adds_messages_and_attachments(tmp_path: Path) -> None:
    config = RuntimeConfig(data_root=tmp_path / "data")
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_self",
        sender_name="Me",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text="看附件",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[AttachmentMeta(attachment_id="att_1", file_name="a.txt", mime_type="text/plain", file_size=1)],
        is_system=False,
    )
    conversation_slice = ConversationSlice(
        slice_id="slice-1",
        conversation_id="oc_1",
        conversation_name="项目群",
        anchor_message_ids=["om_1"],
        in_day_message_ids=["om_1"],
        messages=[message],
        attachment_texts=[],
    )
    requests = [
        ContextRequest(
            slice_id="slice-1",
            request_type=ContextRequestType.ATTACHMENT_TEXT.value,
            target_message_ids=["om_1"],
            target_attachment_ids=["att_1"],
            reason="看正文",
            limit=1,
        ),
        ContextRequest(
            slice_id="slice-1",
            request_type=ContextRequestType.EARLIER_MESSAGES.value,
            target_message_ids=["om_1"],
            target_attachment_ids=[],
            reason="补前文",
            limit=1,
        ),
    ]

    expanded = expand_slice_context(
        conversation_slice,
        requests,
        chat_source=FakeChatSource(),
        content_resolver=FeishuMessageContentResolver(config=config),
        config=config,
    )

    assert len(expanded.messages) == 2
    assert len(expanded.attachment_texts) == 1
