from __future__ import annotations

from pathlib import Path

from src.worktrace.config import RuntimeConfig
from src.worktrace.constants import ContextRequestType
from src.worktrace.models import (
    AnchorUnit,
    AttachmentMeta,
    AttachmentTextBlock,
    ContextRequest,
    NormalizedMessage,
)
from src.worktrace.pipeline.anchor_expansion import expand_anchor_unit_context


class FakeChatSource:
    def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
        return [
            NormalizedMessage(
                conversation_id=conversation_id,
                conversation_name="项目群",
                message_id="om_2",
                sender_open_id="ou_other",
                sender_name="Bob",
                send_time="2026-06-23T10:02:00+08:00",
                message_type="text",
                text="补充了前文说明",
                reply_to_message_id=None,
                quote_message_id=None,
                links=[],
                attachments=[],
                is_system=False,
            )
        ]


class FakeResolver:
    def to_text(self, message):
        return message.text

    def load_attachment_text_if_needed(self, message, attachment_ids, hint):
        return [
            AttachmentTextBlock(
                attachment_id="att_1",
                message_id=message.message_id,
                file_name="voice.m4a",
                text=f"转写内容: {hint}",
            )
        ]


def test_expand_anchor_unit_context_adds_messages_and_attachments(tmp_path: Path) -> None:
    config = RuntimeConfig(data_root=tmp_path / "data")
    anchor_message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_self",
        sender_name="Me",
        send_time="2026-06-23T10:00:00+08:00",
        message_type="audio",
        text="<audio key=\"att_1\" duration=\"10s\"/>",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[
            AttachmentMeta(
                attachment_id="att_1",
                file_name="voice.m4a",
                mime_type="audio/*",
                file_size=1,
            )
        ],
        is_system=False,
    )
    anchor_unit = AnchorUnit(
        anchor_unit_id="oc_1:om_1",
        conversation_id="oc_1",
        conversation_name="项目群",
        anchor_message_ids=["om_1"],
        in_day_message_ids=["om_1"],
        base_message_ids=["om_1"],
        messages=[anchor_message],
        reply_relation_ids=[],
        quote_relation_ids=[],
        attachment_refs=anchor_message.attachments,
    )
    requests = [
        ContextRequest(
            slice_id="oc_1:om_1",
            request_type=ContextRequestType.ATTACHMENT_TEXT.value,
            target_message_ids=["om_1"],
            target_attachment_ids=["att_1"],
            reason="需要语音转写",
            limit=1,
        ),
        ContextRequest(
            slice_id="oc_1:om_1",
            request_type=ContextRequestType.LATER_MESSAGES.value,
            target_message_ids=["om_1"],
            target_attachment_ids=[],
            reason="需要后文",
            limit=1,
        ),
    ]

    expanded, new_messages, all_attachment_texts, new_attachment_texts = expand_anchor_unit_context(
        anchor_unit,
        requests,
        chat_source=FakeChatSource(),
        content_resolver=FakeResolver(),
        config=config,
        existing_attachment_texts=[],
    )

    assert [item.message_id for item in expanded.messages] == ["om_1", "om_2"]
    assert [item.message_id for item in new_messages] == ["om_2"]
    assert len(all_attachment_texts) == 1
    assert len(new_attachment_texts) == 1
    assert all_attachment_texts[0].attachment_id == "att_1"


def test_expand_anchor_unit_context_ignores_invalid_message_ids(tmp_path: Path) -> None:
    config = RuntimeConfig(data_root=tmp_path / "data")
    anchor_message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_self",
        sender_name="Me",
        send_time="2026-06-23T10:00:00+08:00",
        message_type="text",
        text="先看这里",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    anchor_unit = AnchorUnit(
        anchor_unit_id="oc_1:om_1",
        conversation_id="oc_1",
        conversation_name="项目群",
        anchor_message_ids=["om_1"],
        in_day_message_ids=["om_1"],
        base_message_ids=["om_1"],
        messages=[anchor_message],
        reply_relation_ids=[],
        quote_relation_ids=[],
        attachment_refs=[],
    )
    requests = [
        ContextRequest(
            slice_id="oc_1:om_1",
            request_type=ContextRequestType.LATER_MESSAGES.value,
            target_message_ids=["m4", "om_1"],
            target_attachment_ids=[],
            reason="需要后文",
            limit=1,
        )
    ]

    expanded, new_messages, all_attachment_texts, new_attachment_texts = expand_anchor_unit_context(
        anchor_unit,
        requests,
        chat_source=FakeChatSource(),
        content_resolver=FakeResolver(),
        config=config,
        existing_attachment_texts=[],
    )

    assert [item.message_id for item in expanded.messages] == ["om_1", "om_2"]
    assert [item.message_id for item in new_messages] == ["om_2"]
    assert all_attachment_texts == []
    assert new_attachment_texts == []
