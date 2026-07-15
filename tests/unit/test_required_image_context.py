from __future__ import annotations

from dataclasses import replace

from src.worktrace.models import (
    AnchorUnit,
    AttachmentMeta,
    AttachmentTextBlock,
    ConversationSegmentUnit,
    NormalizedMessage,
)
from src.worktrace.pipeline.required_image_context import enrich_required_image_context
from src.worktrace.runner import _attach_anchor_attachment_texts


def _message(
    message_id: str,
    sender_open_id: str,
    *,
    reply_to: str | None = None,
    attachments: list[AttachmentMeta] | None = None,
    minute: int = 0,
) -> NormalizedMessage:
    return NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id=message_id,
        sender_open_id=sender_open_id,
        sender_name=sender_open_id,
        send_time=f"2026-07-13T10:{minute:02d}:00+08:00",
        message_type="image" if attachments else "text",
        text="[图片]" if attachments else "回复图片",
        reply_to_message_id=reply_to,
        quote_message_id=None,
        attachments=attachments or [],
    )


def _unit(messages: list[NormalizedMessage]) -> AnchorUnit:
    return AnchorUnit(
        anchor_unit_id="oc_1:window-001",
        conversation_id="oc_1",
        conversation_name="项目群",
        anchor_message_ids=["om_self"],
        in_day_message_ids=["om_self"],
        base_message_ids=["om_self"],
        messages=messages,
    )


class _Source:
    def __init__(self, parents: list[NormalizedMessage]) -> None:
        self.parents = parents
        self.requests: list[list[str]] = []

    def fetch_messages_by_ids(self, conversation_id, message_ids):
        self.requests.append(message_ids)
        return [item for item in self.parents if item.message_id in message_ids]


class _Resolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def load_required_image_summaries(self, message, attachment_ids):
        self.calls.append((message.message_id, attachment_ids))
        return [
            AttachmentTextBlock(
                attachment_id=attachment_id,
                message_id=message.message_id,
                file_name="image.png",
                text=f"图片内容摘要：{message.message_id}",
            )
            for attachment_id in attachment_ids
        ]


def test_enriches_self_sent_images_and_parent_images_replied_by_self() -> None:
    parent = _message(
        "om_parent",
        "ou_other",
        attachments=[AttachmentMeta("img_parent", "parent.png", "image/png", 1)],
        minute=0,
    )
    self_message = _message(
        "om_self",
        "ou_self",
        reply_to="om_parent",
        attachments=[AttachmentMeta("img_self", "self.png", "image/png", 1)],
        minute=1,
    )
    source = _Source([parent])
    resolver = _Resolver()

    enriched = enrich_required_image_context(
        [_unit([self_message])],
        self_open_id="ou_self",
        chat_source=source,
        content_resolver=resolver,
    )

    assert source.requests == [["om_parent"]]
    assert [message.message_id for message in enriched[0].messages] == ["om_parent", "om_self"]
    assert enriched[0].relation_context_message_ids == ["om_parent"]
    assert resolver.calls == [
        ("om_parent", ["img_parent"]),
        ("om_self", ["img_self"]),
    ]
    assert {(item.message_id, item.attachment_id) for item in enriched[0].attachment_texts} == {
        ("om_parent", "img_parent"),
        ("om_self", "img_self"),
    }


def test_ignores_other_images_without_a_direct_self_reply_or_quote() -> None:
    other_image = _message(
        "om_other",
        "ou_other",
        attachments=[AttachmentMeta("img_other", "other.png", "image/png", 1)],
    )
    self_message = _message("om_self", "ou_self")
    resolver = _Resolver()

    enriched = enrich_required_image_context(
        [_unit([other_image, self_message])],
        self_open_id="ou_self",
        chat_source=_Source([]),
        content_resolver=resolver,
    )

    assert resolver.calls == []
    assert enriched[0].attachment_texts == []


def test_required_image_summaries_are_attached_to_event_extraction_segments() -> None:
    message = _message(
        "om_self",
        "ou_self",
        attachments=[AttachmentMeta("img_self", "self.png", "image/png", 1)],
    )
    unit = _unit([message])
    unit = replace(
        unit,
        attachment_texts=[
            AttachmentTextBlock(
                attachment_id="img_self",
                message_id="om_self",
                file_name="self.png",
                text="图片内容摘要：已确认发布计划。",
            )
        ],
    )
    segment = ConversationSegmentUnit(
        segment_id="turn-001",
        conversation_id="oc_1",
        conversation_name="项目群",
        primary_message_ids=["om_self"],
        context_message_ids=[],
        self_evidence_message_ids=["om_self"],
        response_signals=[],
        response_assessments=[],
        messages=[message],
    )

    attached = _attach_anchor_attachment_texts([segment], unit)

    assert attached[0].attachment_texts == unit.attachment_texts
