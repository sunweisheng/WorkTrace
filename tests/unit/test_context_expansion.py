from __future__ import annotations

from pathlib import Path

from src.worktrace.config import RuntimeConfig
from src.worktrace.constants import ContextDirection, ContextRequestType
from src.worktrace.models import (
    AttachmentMeta,
    ContextRequest,
    ConversationSlice,
    LinkMeta,
    LinkedFileTextBlock,
    NormalizedMessage,
)
from src.worktrace.pipeline.context_expansion import expand_slice_context
from src.worktrace.resolvers.feishu_message import FeishuMessageContentResolver


class FakeChatSource:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], object, int]] = []

    def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
        self.calls.append((conversation_id, target_message_ids, direction, limit))
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


def test_expand_slice_context_adds_relation_object_for_new_temporal_message(tmp_path: Path) -> None:
    class RelationSource(FakeChatSource):
        def __init__(self) -> None:
            super().__init__()
            self.related_object_calls: list[tuple[str, list[str]]] = []

        def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
            self.calls.append((conversation_id, target_message_ids, direction, limit))
            return [
                NormalizedMessage(
                    conversation_id=conversation_id,
                    conversation_name="项目群",
                    message_id="om_2",
                    sender_open_id="ou_other",
                    sender_name="Bob",
                    send_time="2026-06-22T10:01:00+08:00",
                    message_type="text",
                    text="这是对前文的补充",
                    reply_to_message_id="om_parent",
                    quote_message_id=None,
                    links=[],
                    attachments=[],
                    is_system=False,
                )
            ]

        def fetch_messages_by_ids(self, conversation_id, message_ids):
            self.related_object_calls.append((conversation_id, message_ids))
            return [
                NormalizedMessage(
                    conversation_id=conversation_id,
                    conversation_name="项目群",
                    message_id="om_parent",
                    sender_open_id="ou_other",
                    sender_name="Alice",
                    send_time="2026-06-22T09:59:00+08:00",
                    message_type="text",
                    text="被回复的前文",
                    reply_to_message_id=None,
                    quote_message_id=None,
                    links=[],
                    attachments=[],
                    is_system=False,
                )
            ]

    original = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_self",
        sender_name="Me",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text="请补充前文",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    source = RelationSource()
    expanded = expand_slice_context(
        ConversationSlice(
            slice_id="slice-1",
            conversation_id="oc_1",
            conversation_name="项目群",
            anchor_message_ids=["om_1"],
            in_day_message_ids=["om_1"],
            messages=[original],
        ),
        [
            ContextRequest(
                slice_id="slice-1",
                request_type=ContextRequestType.LATER_MESSAGES.value,
                target_message_ids=["om_1"],
                reason="补充后文",
            )
        ],
        chat_source=source,
        content_resolver=FeishuMessageContentResolver(config=RuntimeConfig(data_root=tmp_path / "data")),
        config=RuntimeConfig(data_root=tmp_path / "data"),
    )

    assert [item.message_id for item in expanded.messages] == ["om_parent", "om_1", "om_2"]
    assert source.calls == [("oc_1", ["om_1"], ContextDirection.LATER, 7)]
    assert source.related_object_calls == [("oc_1", ["om_parent"])]


def test_expand_slice_context_adds_messages_and_attachments(tmp_path: Path) -> None:
    config = RuntimeConfig(data_root=tmp_path / "data")
    chat_source = FakeChatSource()
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

    class FakeExtractor:
        def is_supported(self, file_name: str) -> bool:
            return file_name == "a.txt"

        def extract(self, path: Path, *, file_name: str) -> str:
            return path.read_text(encoding="utf-8")

    def fake_download(_message, _attachment_id):
        path = tmp_path / "a.txt"
        path.write_text("附件正文", encoding="utf-8")
        return path

    expanded = expand_slice_context(
        conversation_slice,
        requests,
        chat_source=chat_source,
        content_resolver=FeishuMessageContentResolver(
            config=config,
            text_attachment_extractor=FakeExtractor(),
            attachment_downloader=fake_download,
        ),
        config=config,
    )

    assert len(expanded.messages) == 2
    assert len(expanded.attachment_texts) == 1
    assert expanded.attachment_texts[0].text == "附件正文：附件正文"
    assert chat_source.calls == [("oc_1", ["om_1"], ContextDirection.EARLIER, 7)]


def test_expand_slice_context_ignores_empty_related_request(tmp_path: Path) -> None:
    config = RuntimeConfig(data_root=tmp_path / "data")
    chat_source = FakeChatSource()
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_self",
        sender_name="Me",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text="hello",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
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

    expanded = expand_slice_context(
        conversation_slice,
        [
            ContextRequest(
                slice_id="slice-1",
                request_type=ContextRequestType.LATER_MESSAGES.value,
                target_message_ids=["", "   "],
                target_attachment_ids=[],
                reason="bad request",
                limit=1,
            )
        ],
        chat_source=chat_source,
        content_resolver=FeishuMessageContentResolver(config=config),
        config=config,
    )

    assert expanded == conversation_slice
    assert chat_source.calls == []


def test_expand_slice_context_loads_linked_file_texts(tmp_path: Path) -> None:
    class FakeResolver:
        def to_text(self, message):
            return message.text

        def extract_links(self, message):
            return list(message.links)

        def load_attachment_text_if_needed(self, message, attachment_ids, hint):
            return []

        def load_link_text_if_needed(self, message, link_ids, hint):
            return [
                LinkedFileTextBlock(
                    link_id=link_ids[0],
                    message_id=message.message_id,
                    title="方案",
                    url="https://foo.feishu.cn/docx/abc",
                    text=f"文档正文: {hint}",
                )
            ]

    config = RuntimeConfig(data_root=tmp_path / "data")
    chat_source = FakeChatSource()
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_self",
        sender_name="Me",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text="请看文档",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[LinkMeta(url="https://foo.feishu.cn/docx/abc", title="方案", link_type="feishu_doc")],
        attachments=[],
        is_system=False,
    )
    conversation_slice = ConversationSlice(
        slice_id="slice-1",
        conversation_id="oc_1",
        conversation_name="项目群",
        anchor_message_ids=["om_1"],
        in_day_message_ids=["om_1"],
        messages=[message],
    )

    expanded = expand_slice_context(
        conversation_slice,
        [
            ContextRequest(
                slice_id="slice-1",
                request_type=ContextRequestType.LINKED_FILE_TEXT.value,
                target_message_ids=["om_1"],
                target_link_ids=["om_1#link1"],
                reason="补充文档正文",
                limit=1,
            )
        ],
        chat_source=chat_source,
        content_resolver=FakeResolver(),
        config=config,
    )

    assert len(expanded.linked_file_texts) == 1
    assert expanded.linked_file_texts[0].link_id == "om_1#link1"
