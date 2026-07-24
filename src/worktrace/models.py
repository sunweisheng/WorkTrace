from __future__ import annotations

from dataclasses import dataclass, field
import ast
from typing import Any

from .constants import AnchorStatus


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError("Expected a list.")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError("Expected list items to be strings.")
        result.append(item)
    return result


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError("Expected a list.")
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise TypeError("Expected list items to be dictionaries.")
        result.append(item)
    return result


@dataclass(frozen=True)
class SelfIdentity:
    open_id: str
    display_name: str
    source: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SelfIdentity:
        return cls(
            open_id=str(data["open_id"]),
            display_name=str(data["display_name"]),
            source=str(data["source"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "open_id": self.open_id,
            "display_name": self.display_name,
            "source": self.source,
        }


@dataclass(frozen=True)
class ConversationRef:
    conversation_id: str
    conversation_name: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConversationRef:
        return cls(
            conversation_id=str(data["conversation_id"]),
            conversation_name=str(data["conversation_name"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "conversation_name": self.conversation_name,
        }


@dataclass(frozen=True)
class LinkMeta:
    url: str
    title: str
    link_type: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LinkMeta:
        return cls(
            url=str(data["url"]),
            title=str(data.get("title", "")),
            link_type=str(data["link_type"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "link_type": self.link_type,
        }


@dataclass(frozen=True)
class EventFileLink:
    url: str
    title: str
    link_type: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EventFileLink:
        return cls(
            url=str(data["url"]),
            title=str(data.get("title", "")),
            link_type=str(data["link_type"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "link_type": self.link_type,
        }


@dataclass(frozen=True)
class AttachmentMeta:
    attachment_id: str
    file_name: str
    mime_type: str
    file_size: int | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AttachmentMeta:
        file_size = data.get("file_size")
        if file_size is not None:
            file_size = int(file_size)
        return cls(
            attachment_id=str(data["attachment_id"]),
            file_name=str(data["file_name"]),
            mime_type=str(data["mime_type"]),
            file_size=file_size,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "attachment_id": self.attachment_id,
            "file_name": self.file_name,
            "mime_type": self.mime_type,
            "file_size": self.file_size,
        }


@dataclass(frozen=True)
class MessageReaction:
    reaction_id: str
    operator_open_id: str
    emoji_type: str
    action_time: str
    emoji_name: str = ""
    emoji_description: str = ""
    semantic: str = "unknown"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MessageReaction:
        operator = data.get("operator") if isinstance(data.get("operator"), dict) else {}
        return cls(
            reaction_id=str(data.get("reaction_id", "")),
            operator_open_id=str(
                data.get("operator_open_id")
                or data.get("operator_id")
                or operator.get("operator_id")
                or operator.get("open_id")
                or ""
            ),
            emoji_type=str(
                data.get("emoji_type")
                or (
                    data.get("reaction_type", {}).get("emoji_type")
                    if isinstance(data.get("reaction_type"), dict)
                    else ""
                )
            ),
            action_time=str(data.get("action_time", "")),
            emoji_name=str(data.get("emoji_name", "")),
            emoji_description=str(data.get("emoji_description", "")),
            semantic=str(data.get("semantic", "unknown")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "reaction_id": self.reaction_id,
            "operator_open_id": self.operator_open_id,
            "emoji_type": self.emoji_type,
            "action_time": self.action_time,
            "emoji_name": self.emoji_name,
            "emoji_description": self.emoji_description,
            "semantic": self.semantic,
        }


@dataclass(frozen=True)
class NormalizedMessage:
    conversation_id: str
    conversation_name: str
    message_id: str
    sender_open_id: str | None
    sender_name: str
    send_time: str
    message_type: str
    text: str
    reply_to_message_id: str | None
    quote_message_id: str | None
    conversation_mode: str = ""
    links: list[LinkMeta] = field(default_factory=list)
    attachments: list[AttachmentMeta] = field(default_factory=list)
    is_system: bool = False
    mentioned_open_ids: list[str] = field(default_factory=list)
    reactions: list[MessageReaction] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NormalizedMessage:
        sender_open_id = data.get("sender_open_id")
        reply_to_message_id = data.get("reply_to_message_id")
        quote_message_id = data.get("quote_message_id")
        return cls(
            conversation_id=str(data["conversation_id"]),
            conversation_name=str(data["conversation_name"]),
            message_id=str(data["message_id"]),
            sender_open_id=None if sender_open_id is None else str(sender_open_id),
            sender_name=str(data.get("sender_name", "")),
            send_time=str(data["send_time"]),
            message_type=str(data["message_type"]),
            text=str(data.get("text", "")),
            reply_to_message_id=(
                None if reply_to_message_id is None else str(reply_to_message_id)
            ),
            quote_message_id=(
                None if quote_message_id is None else str(quote_message_id)
            ),
            conversation_mode=str(data.get("conversation_mode", "")),
            links=[LinkMeta.from_dict(item) for item in _dict_list(data.get("links"))],
            attachments=[
                AttachmentMeta.from_dict(item)
                for item in _dict_list(data.get("attachments"))
            ],
            is_system=bool(data.get("is_system", False)),
            mentioned_open_ids=_string_list(data.get("mentioned_open_ids")),
            reactions=[
                MessageReaction.from_dict(item)
                for item in _dict_list(data.get("reactions"))
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "conversation_name": self.conversation_name,
            "message_id": self.message_id,
            "sender_open_id": self.sender_open_id,
            "sender_name": self.sender_name,
            "send_time": self.send_time,
            "message_type": self.message_type,
            "text": self.text,
            "reply_to_message_id": self.reply_to_message_id,
            "quote_message_id": self.quote_message_id,
            "conversation_mode": self.conversation_mode,
            "links": [item.to_dict() for item in self.links],
            "attachments": [item.to_dict() for item in self.attachments],
            "is_system": self.is_system,
            "mentioned_open_ids": list(self.mentioned_open_ids),
            "reactions": [item.to_dict() for item in self.reactions],
        }


@dataclass(frozen=True)
class AttachmentTextBlock:
    attachment_id: str
    message_id: str
    file_name: str
    text: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AttachmentTextBlock:
        return cls(
            attachment_id=str(data["attachment_id"]),
            message_id=str(data["message_id"]),
            file_name=str(data["file_name"]),
            text=str(data.get("text", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "attachment_id": self.attachment_id,
            "message_id": self.message_id,
            "file_name": self.file_name,
            "text": self.text,
        }


@dataclass(frozen=True)
class LinkedFileTextBlock:
    link_id: str
    message_id: str
    title: str
    url: str
    text: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LinkedFileTextBlock:
        return cls(
            link_id=str(data["link_id"]),
            message_id=str(data["message_id"]),
            title=str(data.get("title", "")),
            url=str(data.get("url", "")),
            text=str(data.get("text", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "link_id": self.link_id,
            "message_id": self.message_id,
            "title": self.title,
            "url": self.url,
            "text": self.text,
        }


@dataclass(frozen=True)
class ConversationSlice:
    slice_id: str
    conversation_id: str
    conversation_name: str
    anchor_message_ids: list[str]
    in_day_message_ids: list[str]
    messages: list[NormalizedMessage]
    attachment_texts: list[AttachmentTextBlock] = field(default_factory=list)
    linked_file_texts: list[LinkedFileTextBlock] = field(default_factory=list)
    primary_message_ids: list[str] = field(default_factory=list)
    context_message_ids: list[str] = field(default_factory=list)
    self_evidence_message_ids: list[str] = field(default_factory=list)
    response_signal_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConversationSlice:
        return cls(
            slice_id=str(data["slice_id"]),
            conversation_id=str(data["conversation_id"]),
            conversation_name=str(data["conversation_name"]),
            anchor_message_ids=_string_list(data.get("anchor_message_ids")),
            in_day_message_ids=_string_list(data.get("in_day_message_ids")),
            messages=[
                NormalizedMessage.from_dict(item)
                for item in _dict_list(data.get("messages"))
            ],
            attachment_texts=[
                AttachmentTextBlock.from_dict(item)
                for item in _dict_list(data.get("attachment_texts"))
            ],
            linked_file_texts=[
                LinkedFileTextBlock.from_dict(item)
                for item in _dict_list(data.get("linked_file_texts"))
            ],
            primary_message_ids=_string_list(data.get("primary_message_ids")),
            context_message_ids=_string_list(data.get("context_message_ids")),
            self_evidence_message_ids=_string_list(data.get("self_evidence_message_ids")),
            response_signal_ids=_string_list(data.get("response_signal_ids")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "slice_id": self.slice_id,
            "conversation_id": self.conversation_id,
            "conversation_name": self.conversation_name,
            "anchor_message_ids": list(self.anchor_message_ids),
            "in_day_message_ids": list(self.in_day_message_ids),
            "messages": [item.to_dict() for item in self.messages],
            "attachment_texts": [item.to_dict() for item in self.attachment_texts],
            "linked_file_texts": [item.to_dict() for item in self.linked_file_texts],
            "primary_message_ids": list(self.primary_message_ids),
            "context_message_ids": list(self.context_message_ids),
            "self_evidence_message_ids": list(self.self_evidence_message_ids),
            "response_signal_ids": list(self.response_signal_ids),
        }


@dataclass(frozen=True)
class AnchorUnit:
    anchor_unit_id: str
    conversation_id: str
    conversation_name: str
    anchor_message_ids: list[str]
    in_day_message_ids: list[str]
    base_message_ids: list[str]
    messages: list[NormalizedMessage]
    relation_context_message_ids: list[str] = field(default_factory=list)
    timeline_context_message_ids: list[str] = field(default_factory=list)
    reply_relation_ids: list[str] = field(default_factory=list)
    quote_relation_ids: list[str] = field(default_factory=list)
    attachment_refs: list[AttachmentMeta] = field(default_factory=list)
    anchor_signals: list["AnchorSignal"] = field(default_factory=list)
    attachment_texts: list[AttachmentTextBlock] = field(default_factory=list)
    linked_file_texts: list[LinkedFileTextBlock] = field(default_factory=list)
    oversized_singleton: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AnchorUnit:
        return cls(
            anchor_unit_id=str(data["anchor_unit_id"]),
            conversation_id=str(data["conversation_id"]),
            conversation_name=str(data.get("conversation_name", "")),
            anchor_message_ids=_string_list(data.get("anchor_message_ids")),
            in_day_message_ids=_string_list(data.get("in_day_message_ids")),
            base_message_ids=_string_list(data.get("base_message_ids")),
            messages=[
                NormalizedMessage.from_dict(item)
                for item in _dict_list(data.get("messages"))
            ],
            relation_context_message_ids=_string_list(data.get("relation_context_message_ids")),
            timeline_context_message_ids=_string_list(data.get("timeline_context_message_ids")),
            reply_relation_ids=_string_list(data.get("reply_relation_ids")),
            quote_relation_ids=_string_list(data.get("quote_relation_ids")),
            attachment_refs=[
                AttachmentMeta.from_dict(item)
                for item in _dict_list(data.get("attachment_refs"))
            ],
            anchor_signals=[
                AnchorSignal.from_dict(item)
                for item in _dict_list(data.get("anchor_signals"))
            ],
            attachment_texts=[
                AttachmentTextBlock.from_dict(item)
                for item in _dict_list(data.get("attachment_texts"))
            ],
            linked_file_texts=[
                LinkedFileTextBlock.from_dict(item)
                for item in _dict_list(data.get("linked_file_texts"))
            ],
            oversized_singleton=bool(data.get("oversized_singleton", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor_unit_id": self.anchor_unit_id,
            "conversation_id": self.conversation_id,
            "conversation_name": self.conversation_name,
            "anchor_message_ids": list(self.anchor_message_ids),
            "in_day_message_ids": list(self.in_day_message_ids),
            "base_message_ids": list(self.base_message_ids),
            "messages": [item.to_dict() for item in self.messages],
            "relation_context_message_ids": list(self.relation_context_message_ids),
            "timeline_context_message_ids": list(self.timeline_context_message_ids),
            "reply_relation_ids": list(self.reply_relation_ids),
            "quote_relation_ids": list(self.quote_relation_ids),
            "attachment_refs": [item.to_dict() for item in self.attachment_refs],
            "anchor_signals": [item.to_dict() for item in self.anchor_signals],
            "attachment_texts": [item.to_dict() for item in self.attachment_texts],
            "linked_file_texts": [item.to_dict() for item in self.linked_file_texts],
            "oversized_singleton": self.oversized_singleton,
        }


@dataclass(frozen=True)
class AnalysisBatch:
    target_date: str
    batch_id: str
    retry_round: int
    estimated_tokens: int
    slices: list[ConversationSlice]
    self_open_id: str = ""
    self_display_name: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AnalysisBatch:
        return cls(
            target_date=str(data["target_date"]),
            batch_id=str(data["batch_id"]),
            retry_round=int(data.get("retry_round", 0)),
            estimated_tokens=int(data.get("estimated_tokens", 0)),
            self_open_id=str(data.get("self_open_id", "")),
            self_display_name=str(data.get("self_display_name", "")),
            slices=[
                ConversationSlice.from_dict(item)
                for item in _dict_list(data.get("slices"))
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_date": self.target_date,
            "batch_id": self.batch_id,
            "retry_round": self.retry_round,
            "estimated_tokens": self.estimated_tokens,
            "self_open_id": self.self_open_id,
            "self_display_name": self.self_display_name,
            "slices": [item.to_dict() for item in self.slices],
        }


@dataclass(frozen=True)
class ContextRequest:
    slice_id: str
    request_type: str
    target_message_ids: list[str]
    target_attachment_ids: list[str] = field(default_factory=list)
    target_link_ids: list[str] = field(default_factory=list)
    reason: str = ""
    limit: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContextRequest:
        return cls(
            slice_id=str(data.get("slice_id", "")),
            request_type=str(data["request_type"]),
            target_message_ids=_string_list(data.get("target_message_ids")),
            target_attachment_ids=_string_list(data.get("target_attachment_ids")),
            target_link_ids=_string_list(data.get("target_link_ids")),
            reason=str(data.get("reason", "")),
            limit=int(data.get("limit", 1)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "slice_id": self.slice_id,
            "request_type": self.request_type,
            "target_message_ids": list(self.target_message_ids),
            "target_attachment_ids": list(self.target_attachment_ids),
            "target_link_ids": list(self.target_link_ids),
            "reason": self.reason,
            "limit": self.limit,
        }


def _parse_context_requests(value: Any) -> list[ContextRequest]:
    parsed: list[ContextRequest] = []
    for item in _dict_list(value):
        try:
            request = ContextRequest.from_dict(item)
        except (KeyError, TypeError, ValueError):
            continue
        if not request.request_type.strip():
            continue
        if not request.target_message_ids:
            continue
        parsed.append(request)
    return parsed


@dataclass(frozen=True)
class ResponseSignal:
    signal_id: str
    kind: str
    message_id: str
    action_time: str
    emoji_type: str = ""
    emoji_name: str = ""
    emoji_description: str = ""
    semantic: str = "unknown"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResponseSignal:
        return cls(
            signal_id=str(data.get("signal_id", "")),
            kind=str(data.get("kind", "")),
            message_id=str(data.get("message_id", "")),
            action_time=str(data.get("action_time", "")),
            emoji_type=str(data.get("emoji_type", "")),
            emoji_name=str(data.get("emoji_name", "")),
            emoji_description=str(data.get("emoji_description", "")),
            semantic=str(data.get("semantic", "unknown")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "kind": self.kind,
            "message_id": self.message_id,
            "action_time": self.action_time,
            "emoji_type": self.emoji_type,
            "emoji_name": self.emoji_name,
            "emoji_description": self.emoji_description,
            "semantic": self.semantic,
        }


@dataclass(frozen=True)
class AnchorSignal:
    signal_id: str
    kind: str
    message_id: str
    action_time: str
    emoji_type: str = ""
    emoji_name: str = ""
    emoji_description: str = ""
    semantic: str = "unknown"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AnchorSignal:
        return cls(
            signal_id=str(data.get("signal_id", "")),
            kind=str(data.get("kind", "")),
            message_id=str(data.get("message_id", "")),
            action_time=str(data.get("action_time", "")),
            emoji_type=str(data.get("emoji_type", "")),
            emoji_name=str(data.get("emoji_name", "")),
            emoji_description=str(data.get("emoji_description", "")),
            semantic=str(data.get("semantic", "unknown")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "kind": self.kind,
            "message_id": self.message_id,
            "action_time": self.action_time,
            "emoji_type": self.emoji_type,
            "emoji_name": self.emoji_name,
            "emoji_description": self.emoji_description,
            "semantic": self.semantic,
        }


@dataclass(frozen=True)
class ResponseAssessment:
    signal_id: str
    disposition: str
    continuation: str
    evidence_message_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResponseAssessment:
        return cls(
            signal_id=str(data.get("signal_id", "")),
            disposition=str(data.get("disposition", "unknown")),
            continuation=str(data.get("continuation", "unknown")),
            evidence_message_ids=_string_list(data.get("evidence_message_ids")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "disposition": self.disposition,
            "continuation": self.continuation,
            "evidence_message_ids": list(self.evidence_message_ids),
        }


@dataclass(frozen=True)
class ConversationSegment:
    segment_id: str
    primary_message_ids: list[str]
    context_message_ids: list[str] = field(default_factory=list)
    self_evidence_message_ids: list[str] = field(default_factory=list)
    response_assessments: list[ResponseAssessment] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConversationSegment:
        return cls(
            segment_id=str(data.get("segment_id", "")),
            primary_message_ids=_string_list(data.get("primary_message_ids")),
            context_message_ids=_string_list(data.get("context_message_ids")),
            self_evidence_message_ids=_string_list(data.get("self_evidence_message_ids")),
            response_assessments=[
                ResponseAssessment.from_dict(item)
                for item in _dict_list(data.get("response_assessments"))
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "primary_message_ids": list(self.primary_message_ids),
            "context_message_ids": list(self.context_message_ids),
            "self_evidence_message_ids": list(self.self_evidence_message_ids),
            "response_assessments": [item.to_dict() for item in self.response_assessments],
        }


@dataclass(frozen=True)
class ConversationSegmentationResult:
    # Online analyzers return only the messages that begin a turn.  Python expands
    # those positions against the immutable input timeline into contiguous segments.
    segment_start_message_ids: list[str] = field(default_factory=list)
    segments: list[ConversationSegment] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConversationSegmentationResult:
        return cls(
            segment_start_message_ids=_string_list(
                data.get("segment_start_message_ids")
            ),
            segments=[
                ConversationSegment.from_dict(item)
                for item in _dict_list(data.get("segments"))
            ]
        )

    def to_dict(self) -> dict[str, Any]:
        if self.segment_start_message_ids:
            return {
                "segment_start_message_ids": list(self.segment_start_message_ids),
            }
        return {"segments": [item.to_dict() for item in self.segments]}


@dataclass(frozen=True)
class ConversationSegmentUnit:
    segment_id: str
    conversation_id: str
    conversation_name: str
    primary_message_ids: list[str]
    context_message_ids: list[str]
    self_evidence_message_ids: list[str]
    response_signals: list[ResponseSignal]
    response_assessments: list[ResponseAssessment]
    messages: list[NormalizedMessage]
    attachment_texts: list[AttachmentTextBlock] = field(default_factory=list)
    linked_file_texts: list[LinkedFileTextBlock] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConversationSegmentUnit:
        return cls(
            segment_id=str(data.get("segment_id", "")),
            conversation_id=str(data.get("conversation_id", "")),
            conversation_name=str(data.get("conversation_name", "")),
            primary_message_ids=_string_list(data.get("primary_message_ids")),
            context_message_ids=_string_list(data.get("context_message_ids")),
            self_evidence_message_ids=_string_list(data.get("self_evidence_message_ids")),
            response_signals=[ResponseSignal.from_dict(item) for item in _dict_list(data.get("response_signals"))],
            response_assessments=[ResponseAssessment.from_dict(item) for item in _dict_list(data.get("response_assessments"))],
            messages=[NormalizedMessage.from_dict(item) for item in _dict_list(data.get("messages"))],
            attachment_texts=[AttachmentTextBlock.from_dict(item) for item in _dict_list(data.get("attachment_texts"))],
            linked_file_texts=[LinkedFileTextBlock.from_dict(item) for item in _dict_list(data.get("linked_file_texts"))],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "conversation_id": self.conversation_id,
            "conversation_name": self.conversation_name,
            "primary_message_ids": list(self.primary_message_ids),
            "context_message_ids": list(self.context_message_ids),
            "self_evidence_message_ids": list(self.self_evidence_message_ids),
            "response_signals": [item.to_dict() for item in self.response_signals],
            "response_assessments": [item.to_dict() for item in self.response_assessments],
            "messages": [item.to_dict() for item in self.messages],
            "attachment_texts": [item.to_dict() for item in self.attachment_texts],
            "linked_file_texts": [item.to_dict() for item in self.linked_file_texts],
        }


@dataclass(frozen=True)
class SegmentAnalysisBatch:
    target_date: str
    conversation_id: str
    conversation_name: str
    self_open_id: str
    self_display_name: str
    segments: list[ConversationSegmentUnit]
    estimated_input_tokens: int = 0
    input_target_tokens: int = 0
    oversized_singleton: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_date": self.target_date,
            "conversation_id": self.conversation_id,
            "conversation_name": self.conversation_name,
            "self_open_id": self.self_open_id,
            "self_display_name": self.self_display_name,
            "segments": [item.to_dict() for item in self.segments],
            "estimated_input_tokens": self.estimated_input_tokens,
            "input_target_tokens": self.input_target_tokens,
            "oversized_singleton": self.oversized_singleton,
        }


@dataclass(frozen=True)
class SelfRelationEvidence:
    relation: str
    evidence_message_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SelfRelationEvidence:
        return cls(
            relation=str(data.get("relation", "")),
            evidence_message_ids=_string_list(data.get("evidence_message_ids")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "relation": self.relation,
            "evidence_message_ids": list(self.evidence_message_ids),
        }


@dataclass(frozen=True)
class PersonalFactItem:
    field_name: str
    text: str
    evidence_message_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PersonalFactItem:
        return cls(
            field_name=str(data.get("field", "")),
            text=str(data.get("text", "")),
            evidence_message_ids=_string_list(data.get("evidence_message_ids")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field_name,
            "text": self.text,
            "evidence_message_ids": list(self.evidence_message_ids),
        }


@dataclass(frozen=True)
class SourceBackedEventDraft:
    draft_id: str
    date: str
    topic: str
    content: str
    source_message_ids: list[str]
    source_conversation_id: str
    source_slice_id: str
    confidence: float
    self_relations: list[SelfRelationEvidence] = field(default_factory=list)
    action_label: str = ""
    object_hint: str = ""
    retention_reason: str = ""
    retention_detail: str = ""
    referenced_link_ids: list[str] = field(default_factory=list)
    referenced_attachment_ids: list[str] = field(default_factory=list)
    self_evidence_message_ids: list[str] = field(default_factory=list)
    response_outcome: str = "unknown"
    response_signal_ids: list[str] = field(default_factory=list)
    response_evidence_message_ids: list[str] = field(default_factory=list)
    fact_items: list[PersonalFactItem] = field(default_factory=list)
    fact_risk_flags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceBackedEventDraft:
        return cls(
            draft_id=str(data.get("draft_id", "")),
            date=str(data.get("date", "")),
            topic=str(data.get("topic", "")),
            content=str(data.get("content", "")),
            action_label=str(data.get("action_label", "")),
            object_hint=str(data.get("object_hint", "")),
            retention_reason=str(data.get("retention_reason", "")),
            retention_detail=str(data.get("retention_detail", "")),
            referenced_link_ids=_string_list(data.get("referenced_link_ids")),
            referenced_attachment_ids=_string_list(data.get("referenced_attachment_ids")),
            self_evidence_message_ids=_string_list(data.get("self_evidence_message_ids")),
            source_message_ids=_string_list(data.get("source_message_ids")),
            source_conversation_id=str(data.get("source_conversation_id", "")),
            source_slice_id=str(data.get("source_slice_id", "")),
            confidence=float(data.get("confidence", 0.0)),
            self_relations=[
                SelfRelationEvidence.from_dict(item)
                for item in _dict_list(data.get("self_relations"))
            ],
            response_outcome=str(data.get("response_outcome", "unknown")),
            response_signal_ids=_string_list(data.get("response_signal_ids")),
            response_evidence_message_ids=_string_list(data.get("response_evidence_message_ids")),
            fact_items=[
                PersonalFactItem.from_dict(item)
                for item in _dict_list(data.get("fact_items"))
            ],
            fact_risk_flags=_string_list(data.get("fact_risk_flags")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "date": self.date,
            "topic": self.topic,
            "content": self.content,
            "action_label": self.action_label,
            "object_hint": self.object_hint,
            "retention_reason": self.retention_reason,
            "retention_detail": self.retention_detail,
            "referenced_link_ids": list(self.referenced_link_ids),
            "referenced_attachment_ids": list(self.referenced_attachment_ids),
            "self_evidence_message_ids": list(self.self_evidence_message_ids),
            "source_message_ids": list(self.source_message_ids),
            "source_conversation_id": self.source_conversation_id,
            "source_slice_id": self.source_slice_id,
            "confidence": self.confidence,
            "self_relations": [item.to_dict() for item in self.self_relations],
            "response_outcome": self.response_outcome,
            "response_signal_ids": list(self.response_signal_ids),
            "response_evidence_message_ids": list(self.response_evidence_message_ids),
            "fact_items": [item.to_dict() for item in self.fact_items],
            "fact_risk_flags": list(self.fact_risk_flags),
        }


@dataclass(frozen=True)
class RetentionReviewCandidate:
    candidate: SourceBackedEventDraft
    messages: list[NormalizedMessage] = field(default_factory=list)
    allowed_evidence_message_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RetentionReviewBatch:
    target_date: str
    batch_id: str
    candidates: list[RetentionReviewCandidate] = field(default_factory=list)
    estimated_input_tokens: int = 0
    input_target_tokens: int = 0
    oversized_singleton: bool = False


@dataclass(frozen=True)
class RetentionSignalEvidence:
    signal_type: str
    evidence_message_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RetentionSignalEvidence:
        return cls(
            signal_type=str(data.get("type", "")),
            evidence_message_ids=_string_list(data.get("evidence_message_ids")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.signal_type,
            "evidence_message_ids": list(self.evidence_message_ids),
        }


@dataclass(frozen=True)
class RetentionReviewItemResult:
    draft_id: str
    routine_signals: list[RetentionSignalEvidence] = field(default_factory=list)
    substantive_signals: list[RetentionSignalEvidence] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RetentionReviewItemResult:
        return cls(
            draft_id=str(data.get("draft_id", "")),
            routine_signals=[
                RetentionSignalEvidence.from_dict(item)
                for item in _dict_list(data.get("routine_signals"))
            ],
            substantive_signals=[
                RetentionSignalEvidence.from_dict(item)
                for item in _dict_list(data.get("substantive_signals"))
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "routine_signals": [item.to_dict() for item in self.routine_signals],
            "substantive_signals": [
                item.to_dict() for item in self.substantive_signals
            ],
        }


@dataclass(frozen=True)
class RetentionReviewResult:
    results: list[RetentionReviewItemResult] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RetentionReviewResult:
        return cls(
            results=[
                RetentionReviewItemResult.from_dict(item)
                for item in _dict_list(data.get("results"))
            ]
        )

    def to_dict(self) -> dict[str, Any]:
        return {"results": [item.to_dict() for item in self.results]}


@dataclass(frozen=True)
class PersonalFactReviewCandidate:
    candidate: SourceBackedEventDraft
    messages: list[NormalizedMessage] = field(default_factory=list)
    allowed_evidence_message_ids: list[str] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PersonalFactReviewBatch:
    target_date: str
    batch_id: str
    candidates: list[PersonalFactReviewCandidate] = field(default_factory=list)
    retry_feedback: str = ""
    estimated_input_tokens: int = 0
    input_target_tokens: int = 0
    oversized_singleton: bool = False


@dataclass(frozen=True)
class PersonalFactReviewItemResult:
    draft_id: str
    supported: bool
    topic: str = ""
    content: str = ""
    action_label: str = ""
    object_hint: str = ""
    retention_detail: str = ""
    fact_items: list[PersonalFactItem] = field(default_factory=list)
    removed_claims: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PersonalFactReviewItemResult:
        return cls(
            draft_id=str(data.get("draft_id", "")),
            supported=bool(data.get("supported", False)),
            topic=str(data.get("topic", "")),
            content=str(data.get("content", "")),
            action_label=str(data.get("action_label", "")),
            object_hint=str(data.get("object_hint", "")),
            retention_detail=str(data.get("retention_detail", "")),
            fact_items=[
                PersonalFactItem.from_dict(item)
                for item in _dict_list(data.get("fact_items"))
            ],
            removed_claims=_string_list(data.get("removed_claims")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "supported": self.supported,
            "topic": self.topic,
            "content": self.content,
            "action_label": self.action_label,
            "object_hint": self.object_hint,
            "retention_detail": self.retention_detail,
            "fact_items": [item.to_dict() for item in self.fact_items],
            "removed_claims": list(self.removed_claims),
        }


@dataclass(frozen=True)
class PersonalFactReviewResult:
    results: list[PersonalFactReviewItemResult] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PersonalFactReviewResult:
        return cls(
            results=[
                PersonalFactReviewItemResult.from_dict(item)
                for item in _dict_list(data.get("results"))
            ]
        )

    def to_dict(self) -> dict[str, Any]:
        return {"results": [item.to_dict() for item in self.results]}


@dataclass(frozen=True)
class BatchAnalysisResult:
    candidate_events: list[SourceBackedEventDraft] = field(default_factory=list)
    context_requests: list[ContextRequest] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatchAnalysisResult:
        return cls(
            candidate_events=[
                SourceBackedEventDraft.from_dict(item)
                for item in _dict_list(data.get("candidate_events"))
            ],
            context_requests=_parse_context_requests(data.get("context_requests")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_events": [item.to_dict() for item in self.candidate_events],
            "context_requests": [item.to_dict() for item in self.context_requests],
        }


@dataclass(frozen=True)
class BatchSegmentAnalysisItem:
    segment_id: str
    analysis: BatchAnalysisResult

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatchSegmentAnalysisItem:
        return cls(
            segment_id=str(data.get("segment_id", "")),
            analysis=BatchAnalysisResult.from_dict(
                data.get("analysis") if isinstance(data.get("analysis"), dict) else {}
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "analysis": self.analysis.to_dict(),
        }


@dataclass(frozen=True)
class BatchSegmentAnalysisResult:
    results: list[BatchSegmentAnalysisItem] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatchSegmentAnalysisResult:
        return cls(
            results=[
                BatchSegmentAnalysisItem.from_dict(item)
                for item in _dict_list(data.get("results"))
            ]
        )

    def to_dict(self) -> dict[str, Any]:
        return {"results": [item.to_dict() for item in self.results]}


@dataclass(frozen=True)
class AnchorAnalysisResult:
    anchor_status: str
    candidate_events: list[SourceBackedEventDraft] = field(default_factory=list)
    context_requests: list[ContextRequest] = field(default_factory=list)
    needs_cross_anchor_merge: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AnchorAnalysisResult:
        return cls(
            anchor_status=_normalize_anchor_status(data.get("anchor_status", "")),
            candidate_events=[
                SourceBackedEventDraft.from_dict(item)
                for item in _dict_list(data.get("candidate_events"))
            ],
            context_requests=_parse_context_requests(data.get("context_requests")),
            needs_cross_anchor_merge=bool(data.get("needs_cross_anchor_merge", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor_status": self.anchor_status,
            "candidate_events": [item.to_dict() for item in self.candidate_events],
            "context_requests": [item.to_dict() for item in self.context_requests],
            "needs_cross_anchor_merge": self.needs_cross_anchor_merge,
        }


_ANCHOR_STATUS_PRIORITY: dict[str, int] = {
    AnchorStatus.NEEDS_ATTACHMENT_TEXT.value: 0,
    AnchorStatus.NEEDS_MORE_CONTEXT.value: 1,
    AnchorStatus.UNCERTAIN.value: 2,
    AnchorStatus.FAILED.value: 3,
    AnchorStatus.PENDING.value: 4,
    AnchorStatus.COMPLETED.value: 5,
    AnchorStatus.NOT_WORK_RELATED.value: 6,
    AnchorStatus.SKIPPED.value: 7,
}


def _normalize_anchor_status(value: Any) -> str:
    candidates = _extract_anchor_status_candidates(value)
    if not candidates:
        return str(value or "")

    unique_candidates: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if item in seen:
            continue
        seen.add(item)
        unique_candidates.append(item)

    return min(
        unique_candidates,
        key=lambda item: (_ANCHOR_STATUS_PRIORITY.get(item, 999), item),
    )


def _extract_anchor_status_candidates(value: Any) -> list[str]:
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped in _ANCHOR_STATUS_PRIORITY:
            return [stripped]
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                parsed = ast.literal_eval(stripped)
            except (SyntaxError, ValueError):
                return [stripped]
            return _extract_anchor_status_candidates(parsed)
        return [stripped]

    if isinstance(value, list | tuple | set):
        results: list[str] = []
        for item in value:
            results.extend(_extract_anchor_status_candidates(item))
        return results

    if value is None:
        return []
    return [str(value)]


@dataclass(frozen=True)
class BatchAnchorAnalysisItem:
    anchor_unit_id: str
    analysis: AnchorAnalysisResult

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatchAnchorAnalysisItem:
        return cls(
            anchor_unit_id=str(data["anchor_unit_id"]),
            analysis=AnchorAnalysisResult.from_dict(data["analysis"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor_unit_id": self.anchor_unit_id,
            "analysis": self.analysis.to_dict(),
        }


@dataclass(frozen=True)
class BatchAnchorAnalysisResult:
    results: list[BatchAnchorAnalysisItem] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatchAnchorAnalysisResult:
        return cls(
            results=[
                BatchAnchorAnalysisItem.from_dict(item)
                for item in _dict_list(data.get("results"))
            ]
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "results": [item.to_dict() for item in self.results],
        }


@dataclass(frozen=True)
class MergedEventDraft:
    date: str
    topic: str
    content: str
    source_message_ids: list[str]
    source_conversation_ids: list[str]
    object_hint: str = ""
    retention_reason: str = ""
    retention_detail: str = ""
    referenced_link_ids: list[str] = field(default_factory=list)
    referenced_attachment_ids: list[str] = field(default_factory=list)
    action_labels: list[str] = field(default_factory=list)
    self_relations: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MergedEventDraft:
        return cls(
            date=str(data["date"]),
            topic=str(data.get("topic", "")),
            content=str(data.get("content", "")),
            source_message_ids=_string_list(data.get("source_message_ids")),
            source_conversation_ids=_string_list(data.get("source_conversation_ids")),
            object_hint=str(data.get("object_hint", "")),
            retention_reason=str(data.get("retention_reason", "")),
            retention_detail=str(data.get("retention_detail", "")),
            referenced_link_ids=_string_list(data.get("referenced_link_ids")),
            referenced_attachment_ids=_string_list(data.get("referenced_attachment_ids")),
            action_labels=_string_list(data.get("action_labels")),
            self_relations=_string_list(data.get("self_relations")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "topic": self.topic,
            "content": self.content,
            "source_message_ids": list(self.source_message_ids),
            "source_conversation_ids": list(self.source_conversation_ids),
            "object_hint": self.object_hint,
            "retention_reason": self.retention_reason,
            "retention_detail": self.retention_detail,
            "referenced_link_ids": list(self.referenced_link_ids),
            "referenced_attachment_ids": list(self.referenced_attachment_ids),
            "action_labels": list(self.action_labels),
            "self_relations": list(self.self_relations),
        }


@dataclass(frozen=True)
class CrossConversationGroup:
    group_id: str
    draft_ids: list[str]
    primary_draft_id: str = ""
    merge_reason: str = ""
    evidence_message_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CrossConversationGroup:
        return cls(
            group_id=str(data.get("group_id", "")),
            draft_ids=_string_list(data.get("draft_ids")),
            primary_draft_id=str(data.get("primary_draft_id", "")),
            merge_reason=str(data.get("merge_reason", "")),
            evidence_message_ids=_string_list(data.get("evidence_message_ids")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "draft_ids": list(self.draft_ids),
            "primary_draft_id": self.primary_draft_id,
            "merge_reason": self.merge_reason,
            "evidence_message_ids": list(self.evidence_message_ids),
        }


@dataclass(frozen=True)
class CrossConversationGroupResult:
    groups: list[CrossConversationGroup] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CrossConversationGroupResult:
        return cls(
            groups=[
                CrossConversationGroup.from_dict(item)
                for item in _dict_list(data.get("groups"))
            ]
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "groups": [item.to_dict() for item in self.groups],
        }


@dataclass(frozen=True)
class WorkEvent:
    date: str
    event_id: str
    title: str
    content: str
    source_message_ids: list[str] = field(default_factory=list)
    file_links: list[EventFileLink] = field(default_factory=list)
    source_people: list[str] = field(default_factory=list)
    source_event_ids: list[str] = field(default_factory=list)
    source_report_owners: list[str] = field(default_factory=list)
    object_hint: str = ""
    retention_reason: str = ""
    retention_detail: str = ""
    referenced_link_ids: list[str] = field(default_factory=list)
    referenced_attachment_ids: list[str] = field(default_factory=list)
    action_labels: list[str] = field(default_factory=list)
    self_relations: list[str] = field(default_factory=list)
    evidence_fingerprints: list[str] = field(default_factory=list)
    conversation_fingerprints: list[str] = field(default_factory=list)
    file_keys: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkEvent:
        return cls(
            date=str(data["date"]),
            event_id=str(data["event_id"]),
            title=str(data.get("title", data.get("topic", ""))),
            content=str(data.get("content", "")),
            source_message_ids=_string_list(data.get("source_message_ids")),
            file_links=[
                EventFileLink.from_dict(item)
                for item in _dict_list(data.get("file_links"))
            ],
            source_people=_string_list(data.get("source_people")),
            source_event_ids=_string_list(data.get("source_event_ids")),
            source_report_owners=_string_list(data.get("source_report_owners")),
            object_hint=str(data.get("object_hint", "")),
            retention_reason=str(data.get("retention_reason", "")),
            retention_detail=str(data.get("retention_detail", "")),
            referenced_link_ids=_string_list(data.get("referenced_link_ids")),
            referenced_attachment_ids=_string_list(data.get("referenced_attachment_ids")),
            action_labels=_string_list(data.get("action_labels")),
            self_relations=_string_list(data.get("self_relations")),
            evidence_fingerprints=_string_list(data.get("evidence_fingerprints")),
            conversation_fingerprints=_string_list(
                data.get("conversation_fingerprints")
            ),
            file_keys=_string_list(data.get("file_keys")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "event_id": self.event_id,
            "title": self.title,
            "topic": self.title,
            "content": self.content,
            "source_message_ids": list(self.source_message_ids),
            "file_links": [item.to_dict() for item in self.file_links],
            "source_people": list(self.source_people),
            "source_event_ids": list(self.source_event_ids),
            "source_report_owners": list(self.source_report_owners),
            "object_hint": self.object_hint,
            "retention_reason": self.retention_reason,
            "retention_detail": self.retention_detail,
            "referenced_link_ids": list(self.referenced_link_ids),
            "referenced_attachment_ids": list(self.referenced_attachment_ids),
            "action_labels": list(self.action_labels),
            "self_relations": list(self.self_relations),
            "evidence_fingerprints": list(self.evidence_fingerprints),
            "conversation_fingerprints": list(self.conversation_fingerprints),
            "file_keys": list(self.file_keys),
        }

    @property
    def topic(self) -> str:
        return self.title


@dataclass(frozen=True)
class DayDocument:
    date: str
    events: list[WorkEvent]
    generated_at: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DayDocument:
        return cls(
            date=str(data["date"]),
            events=[WorkEvent.from_dict(item) for item in _dict_list(data.get("events"))],
            generated_at=str(data["generated_at"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "events": [item.to_dict() for item in self.events],
            "generated_at": self.generated_at,
        }


@dataclass(frozen=True)
class StoreWriteResult:
    output_path: str
    event_count: int
    written_at: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StoreWriteResult:
        return cls(
            output_path=str(data["output_path"]),
            event_count=int(data.get("event_count", 0)),
            written_at=str(data["written_at"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_path": self.output_path,
            "event_count": self.event_count,
            "written_at": self.written_at,
        }


@dataclass(frozen=True)
class AnchorCacheEntry:
    target_date: str
    anchor_unit_id: str
    input_fingerprint: str
    status: str
    pass_index: int
    prompt_version: str
    schema_version: str
    analyzer_key: str
    candidate_events: list[SourceBackedEventDraft] = field(default_factory=list)
    context_requests: list[ContextRequest] = field(default_factory=list)
    included_message_ids: list[str] = field(default_factory=list)
    included_attachment_ids: list[str] = field(default_factory=list)
    included_link_ids: list[str] = field(default_factory=list)
    needs_cross_anchor_merge: bool = False
    created_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AnchorCacheEntry:
        return cls(
            target_date=str(data["target_date"]),
            anchor_unit_id=str(data["anchor_unit_id"]),
            input_fingerprint=str(data["input_fingerprint"]),
            status=str(data["status"]),
            pass_index=int(data.get("pass_index", 0)),
            prompt_version=str(data.get("prompt_version", "v1")),
            schema_version=str(data.get("schema_version", "v1")),
            analyzer_key=str(data.get("analyzer_key", "")),
            candidate_events=[
                SourceBackedEventDraft.from_dict(item)
                for item in _dict_list(data.get("candidate_events"))
            ],
            context_requests=[
                ContextRequest.from_dict(item)
                for item in _dict_list(data.get("context_requests"))
            ],
            included_message_ids=_string_list(data.get("included_message_ids")),
            included_attachment_ids=_string_list(data.get("included_attachment_ids")),
            included_link_ids=_string_list(data.get("included_link_ids")),
            needs_cross_anchor_merge=bool(data.get("needs_cross_anchor_merge", False)),
            created_at=str(data.get("created_at", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_date": self.target_date,
            "anchor_unit_id": self.anchor_unit_id,
            "input_fingerprint": self.input_fingerprint,
            "status": self.status,
            "pass_index": self.pass_index,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "analyzer_key": self.analyzer_key,
            "candidate_events": [item.to_dict() for item in self.candidate_events],
            "context_requests": [item.to_dict() for item in self.context_requests],
            "included_message_ids": list(self.included_message_ids),
            "included_attachment_ids": list(self.included_attachment_ids),
            "included_link_ids": list(self.included_link_ids),
            "needs_cross_anchor_merge": self.needs_cross_anchor_merge,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class RetentionReviewSummary:
    selected_candidate_count: int = 0
    reviewed_candidate_count: int = 0
    kept_candidate_count: int = 0
    dropped_routine_count: int = 0
    dropped_uncertain_count: int = 0
    review_batch_count: int = 0
    review_retry_count: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RetentionReviewSummary:
        return cls(
            selected_candidate_count=int(data.get("selected_candidate_count", 0)),
            reviewed_candidate_count=int(data.get("reviewed_candidate_count", 0)),
            kept_candidate_count=int(data.get("kept_candidate_count", 0)),
            dropped_routine_count=int(data.get("dropped_routine_count", 0)),
            dropped_uncertain_count=int(data.get("dropped_uncertain_count", 0)),
            review_batch_count=int(data.get("review_batch_count", 0)),
            review_retry_count=int(data.get("review_retry_count", 0)),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "selected_candidate_count": self.selected_candidate_count,
            "reviewed_candidate_count": self.reviewed_candidate_count,
            "kept_candidate_count": self.kept_candidate_count,
            "dropped_routine_count": self.dropped_routine_count,
            "dropped_uncertain_count": self.dropped_uncertain_count,
            "review_batch_count": self.review_batch_count,
            "review_retry_count": self.review_retry_count,
        }


@dataclass(frozen=True)
class PersonalFactReviewSummary:
    selected_candidate_count: int = 0
    reviewed_candidate_count: int = 0
    confirmed_candidate_count: int = 0
    revised_candidate_count: int = 0
    dropped_unsupported_count: int = 0
    review_batch_count: int = 0
    review_retry_count: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PersonalFactReviewSummary:
        return cls(
            selected_candidate_count=int(data.get("selected_candidate_count", 0)),
            reviewed_candidate_count=int(data.get("reviewed_candidate_count", 0)),
            confirmed_candidate_count=int(data.get("confirmed_candidate_count", 0)),
            revised_candidate_count=int(data.get("revised_candidate_count", 0)),
            dropped_unsupported_count=int(data.get("dropped_unsupported_count", 0)),
            review_batch_count=int(data.get("review_batch_count", 0)),
            review_retry_count=int(data.get("review_retry_count", 0)),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "selected_candidate_count": self.selected_candidate_count,
            "reviewed_candidate_count": self.reviewed_candidate_count,
            "confirmed_candidate_count": self.confirmed_candidate_count,
            "revised_candidate_count": self.revised_candidate_count,
            "dropped_unsupported_count": self.dropped_unsupported_count,
            "review_batch_count": self.review_batch_count,
            "review_retry_count": self.review_retry_count,
        }


@dataclass(frozen=True)
class DayGroupingSummary:
    candidate_count: int = 0
    initial_group_count: int = 0
    final_group_count: int = 0
    review_component_count: int = 0
    review_request_count: int = 0
    validation_retry_count: int = 0
    codex_fallback_count: int = 0
    singleton_repair_candidate_count: int = 0
    warning_count: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DayGroupingSummary":
        return cls(
            candidate_count=int(data.get("candidate_count", 0)),
            initial_group_count=int(data.get("initial_group_count", 0)),
            final_group_count=int(data.get("final_group_count", 0)),
            review_component_count=int(data.get("review_component_count", 0)),
            review_request_count=int(data.get("review_request_count", 0)),
            validation_retry_count=int(data.get("validation_retry_count", 0)),
            codex_fallback_count=int(data.get("codex_fallback_count", 0)),
            singleton_repair_candidate_count=int(
                data.get("singleton_repair_candidate_count", 0)
            ),
            warning_count=int(data.get("warning_count", 0)),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "candidate_count": self.candidate_count,
            "initial_group_count": self.initial_group_count,
            "final_group_count": self.final_group_count,
            "review_component_count": self.review_component_count,
            "review_request_count": self.review_request_count,
            "validation_retry_count": self.validation_retry_count,
            "codex_fallback_count": self.codex_fallback_count,
            "singleton_repair_candidate_count": self.singleton_repair_candidate_count,
            "warning_count": self.warning_count,
        }


@dataclass(frozen=True)
class DailyRunResult:
    target_date: str
    conversation_count: int
    message_count: int
    slice_count: int
    batch_count: int
    event_count: int
    skipped_slice_count: int
    warning_count: int
    status: str
    output_path: str | None
    error_summary: str
    self_delivery_status: str = ""
    self_delivery_target: str = ""
    self_delivery_error: str = ""
    retention_review_summary: RetentionReviewSummary = field(
        default_factory=RetentionReviewSummary
    )
    personal_fact_review_summary: PersonalFactReviewSummary = field(
        default_factory=PersonalFactReviewSummary
    )
    day_grouping_summary: DayGroupingSummary = field(default_factory=DayGroupingSummary)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DailyRunResult:
        return cls(
            target_date=str(data["target_date"]),
            conversation_count=int(data.get("conversation_count", 0)),
            message_count=int(data.get("message_count", 0)),
            slice_count=int(data.get("slice_count", 0)),
            batch_count=int(data.get("batch_count", 0)),
            event_count=int(data.get("event_count", 0)),
            skipped_slice_count=int(data.get("skipped_slice_count", 0)),
            warning_count=int(data.get("warning_count", 0)),
            status=str(data["status"]),
            output_path=(
                None if data.get("output_path") is None else str(data["output_path"])
            ),
            error_summary=str(data.get("error_summary", "")),
            self_delivery_status=str(data.get("self_delivery_status", "")),
            self_delivery_target=str(data.get("self_delivery_target", "")),
            self_delivery_error=str(data.get("self_delivery_error", "")),
            retention_review_summary=RetentionReviewSummary.from_dict(
                data.get("retention_review_summary", {})
                if isinstance(data.get("retention_review_summary", {}), dict)
                else {}
            ),
            personal_fact_review_summary=PersonalFactReviewSummary.from_dict(
                data.get("personal_fact_review_summary", {})
                if isinstance(data.get("personal_fact_review_summary", {}), dict)
                else {}
            ),
            day_grouping_summary=DayGroupingSummary.from_dict(
                data.get("day_grouping_summary", {})
                if isinstance(data.get("day_grouping_summary", {}), dict)
                else {}
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_date": self.target_date,
            "conversation_count": self.conversation_count,
            "message_count": self.message_count,
            "slice_count": self.slice_count,
            "batch_count": self.batch_count,
            "event_count": self.event_count,
            "skipped_slice_count": self.skipped_slice_count,
            "warning_count": self.warning_count,
            "status": self.status,
            "output_path": self.output_path,
            "error_summary": self.error_summary,
            "self_delivery_status": self.self_delivery_status,
            "self_delivery_target": self.self_delivery_target,
            "self_delivery_error": self.self_delivery_error,
            "retention_review_summary": self.retention_review_summary.to_dict(),
            "personal_fact_review_summary": self.personal_fact_review_summary.to_dict(),
            "day_grouping_summary": self.day_grouping_summary.to_dict(),
        }


@dataclass(frozen=True)
class CollectedSourceEvent:
    draft_id: str
    person_name: str
    source_file: str
    event: WorkEvent
    source_report_owner: str = ""
    is_merge_owner_source: bool = False
    candidate_summary_source: str = ""
    prompt_original_content_chars: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "person_name": self.person_name,
            "source_file": self.source_file,
            "event": self.event.to_dict(),
            "source_report_owner": self.source_report_owner,
            "is_merge_owner_source": self.is_merge_owner_source,
            "candidate_summary_source": self.candidate_summary_source,
            "prompt_original_content_chars": self.prompt_original_content_chars,
        }


@dataclass(frozen=True)
class CollectedGroupMemberConnection:
    draft_id: str
    connection_detail: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CollectedGroupMemberConnection":
        return cls(
            draft_id=str(data.get("draft_id", "")),
            connection_detail=str(data.get("connection_detail", "")),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "draft_id": self.draft_id,
            "connection_detail": self.connection_detail,
        }


@dataclass(frozen=True)
class CollectedGroupingGroup:
    group_id: str
    draft_ids: list[str]
    summary_title: str = ""
    summary_content: str = ""
    summary_object_hint: str = ""
    summary_source: str = ""
    split_reason: str = ""
    group_reason: list[str] = field(default_factory=list)
    semantic_reasons: list[str] = field(default_factory=list)
    evidence_relation_ids: list[str] = field(default_factory=list)
    reason_detail: str = ""
    member_connections: list[CollectedGroupMemberConnection] = field(
        default_factory=list
    )
    risk_flags: list[str] = field(default_factory=list)
    was_repaired: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CollectedGroupingGroup":
        return cls(
            group_id=str(data.get("group_id", "")),
            draft_ids=_string_list(data.get("draft_ids")),
            summary_title=str(data.get("summary_title", "")),
            summary_content=str(data.get("summary_content", "")),
            summary_object_hint=str(data.get("summary_object_hint", "")),
            summary_source=str(data.get("summary_source", "")),
            split_reason=str(data.get("split_reason", "")),
            group_reason=_string_list(data.get("group_reason")),
            semantic_reasons=_string_list(data.get("semantic_reasons")),
            evidence_relation_ids=_string_list(data.get("evidence_relation_ids")),
            reason_detail=str(data.get("reason_detail", "")),
            member_connections=[
                CollectedGroupMemberConnection.from_dict(item)
                for item in _dict_list(data.get("member_connections"))
            ],
            risk_flags=_string_list(data.get("risk_flags")),
            was_repaired=bool(data.get("was_repaired", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "draft_ids": list(self.draft_ids),
            "summary_title": self.summary_title,
            "summary_content": self.summary_content,
            "summary_object_hint": self.summary_object_hint,
            "summary_source": self.summary_source,
            "split_reason": self.split_reason,
            "group_reason": list(self.group_reason),
            "semantic_reasons": list(self.semantic_reasons),
            "evidence_relation_ids": list(self.evidence_relation_ids),
            "reason_detail": self.reason_detail,
            "member_connections": [
                item.to_dict() for item in self.member_connections
            ],
            "risk_flags": list(self.risk_flags),
            "was_repaired": self.was_repaired,
        }


@dataclass(frozen=True)
class CollectedGroupingResult:
    groups: list[CollectedGroupingGroup] = field(default_factory=list)
    split_reason: str = ""
    validation_errors: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CollectedGroupingResult":
        groups = [
            CollectedGroupingGroup.from_dict(item)
            for item in _dict_list(data.get("groups"))
        ]
        split_reason = str(data.get("split_reason", "")).strip()
        return cls(
            groups=groups,
            split_reason=split_reason,
            validation_errors=_string_list(data.get("validation_errors")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "split_reason": self.split_reason,
            "groups": [item.to_dict() for item in self.groups],
            "validation_errors": list(self.validation_errors),
        }


@dataclass(frozen=True)
class CollectedFactItem:
    text: str
    source_draft_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CollectedFactItem:
        return cls(
            text=str(data.get("text", "")),
            source_draft_ids=_string_list(data.get("source_draft_ids")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "source_draft_ids": list(self.source_draft_ids),
        }


@dataclass(frozen=True)
class CollectedMergeGroup:
    group_id: str
    draft_ids: list[str]
    title: str
    content: str
    object_hint: str = ""
    retention_reason: str = ""
    retention_detail: str = ""
    merge_owner_conflict: bool = False
    conflict_detail: str = ""
    covered_draft_ids: list[str] | None = None
    fact_items: list[CollectedFactItem] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CollectedMergeGroup:
        return cls(
            group_id=str(data.get("group_id", "")),
            draft_ids=_string_list(data.get("draft_ids")),
            title=str(data.get("title", data.get("topic", ""))),
            content=str(data.get("content", "")),
            object_hint=str(data.get("object_hint", "")),
            retention_reason=str(data.get("retention_reason", "")),
            retention_detail=str(data.get("retention_detail", "")),
            merge_owner_conflict=bool(data.get("merge_owner_conflict", False)),
            conflict_detail=str(data.get("conflict_detail", "")),
            covered_draft_ids=(
                None
                if "covered_draft_ids" in data
                and data.get("covered_draft_ids") is None
                else _string_list(data.get("covered_draft_ids"))
            ),
            fact_items=[
                CollectedFactItem.from_dict(item)
                for item in _dict_list(data.get("fact_items"))
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "draft_ids": list(self.draft_ids),
            "title": self.title,
            "content": self.content,
            "object_hint": self.object_hint,
            "retention_reason": self.retention_reason,
            "retention_detail": self.retention_detail,
            "merge_owner_conflict": self.merge_owner_conflict,
            "conflict_detail": self.conflict_detail,
            "covered_draft_ids": (
                None
                if self.covered_draft_ids is None
                else list(self.covered_draft_ids)
            ),
            "fact_items": [item.to_dict() for item in self.fact_items],
        }


@dataclass(frozen=True)
class CollectedMergeResult:
    groups: list[CollectedMergeGroup] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CollectedMergeResult:
        return cls(
            groups=[
                CollectedMergeGroup.from_dict(item)
                for item in _dict_list(data.get("groups"))
            ]
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "groups": [item.to_dict() for item in self.groups],
        }


@dataclass(frozen=True)
class CollectedMergeQualitySummary:
    input_event_count: int = 0
    filtered_event_count: int = 0
    output_event_count: int = 0
    multi_source_group_count: int = 0
    singleton_group_count: int = 0
    max_source_events_per_group: int = 0
    input_content_chars: int = 0
    output_content_chars: int = 0
    event_count_output_input_ratio: float = 0.0
    content_chars_output_input_ratio: float = 0.0
    source_event_coverage_ratio: float = 0.0
    source_report_owner_count: int = 0
    high_risk_group_count: int = 0
    reviewed_group_count: int = 0
    review_split_group_count: int = 0
    content_retry_count: int = 0
    shortened_prompt_count: int = 0
    review_required: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CollectedMergeQualitySummary:
        return cls(
            input_event_count=int(data.get("input_event_count", 0)),
            filtered_event_count=int(data.get("filtered_event_count", 0)),
            output_event_count=int(data.get("output_event_count", 0)),
            multi_source_group_count=int(data.get("multi_source_group_count", 0)),
            singleton_group_count=int(data.get("singleton_group_count", 0)),
            max_source_events_per_group=int(
                data.get("max_source_events_per_group", 0)
            ),
            input_content_chars=int(data.get("input_content_chars", 0)),
            output_content_chars=int(data.get("output_content_chars", 0)),
            event_count_output_input_ratio=float(
                data.get("event_count_output_input_ratio", 0.0)
            ),
            content_chars_output_input_ratio=float(
                data.get("content_chars_output_input_ratio", 0.0)
            ),
            source_event_coverage_ratio=float(
                data.get("source_event_coverage_ratio", 0.0)
            ),
            source_report_owner_count=int(data.get("source_report_owner_count", 0)),
            high_risk_group_count=int(data.get("high_risk_group_count", 0)),
            reviewed_group_count=int(data.get("reviewed_group_count", 0)),
            review_split_group_count=int(data.get("review_split_group_count", 0)),
            content_retry_count=int(data.get("content_retry_count", 0)),
            shortened_prompt_count=int(data.get("shortened_prompt_count", 0)),
            review_required=bool(data.get("review_required", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }


@dataclass(frozen=True)
class CollectedMergeOutput:
    input_dir: str
    output_path: str | None
    source_file_count: int
    source_event_count: int
    merged_event_count: int
    skipped_file_count: int
    partial_file_count: int = 0
    quality_summary: CollectedMergeQualitySummary = field(
        default_factory=CollectedMergeQualitySummary
    )
    warning_messages: list[str] = field(default_factory=list)
    self_delivery_status: str = ""
    self_delivery_target: str = ""
    self_delivery_error: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CollectedMergeOutput:
        return cls(
            input_dir=str(data["input_dir"]),
            output_path=(
                None if data.get("output_path") is None else str(data["output_path"])
            ),
            source_file_count=int(data.get("source_file_count", 0)),
            source_event_count=int(data.get("source_event_count", 0)),
            merged_event_count=int(data.get("merged_event_count", 0)),
            skipped_file_count=int(data.get("skipped_file_count", 0)),
            partial_file_count=int(data.get("partial_file_count", 0)),
            quality_summary=CollectedMergeQualitySummary.from_dict(
                data.get("quality_summary", {})
                if isinstance(data.get("quality_summary", {}), dict)
                else {}
            ),
            warning_messages=_string_list(data.get("warning_messages")),
            self_delivery_status=str(
                data.get("self_delivery_status", data.get("upload_status", ""))
            ),
            self_delivery_target=str(
                data.get("self_delivery_target", data.get("upload_target", ""))
            ),
            self_delivery_error=str(
                data.get("self_delivery_error", data.get("upload_error", ""))
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_dir": self.input_dir,
            "output_path": self.output_path,
            "source_file_count": self.source_file_count,
            "source_event_count": self.source_event_count,
            "merged_event_count": self.merged_event_count,
            "skipped_file_count": self.skipped_file_count,
            "partial_file_count": self.partial_file_count,
            "quality_summary": self.quality_summary.to_dict(),
            "warning_messages": list(self.warning_messages),
            "self_delivery_status": self.self_delivery_status,
            "self_delivery_target": self.self_delivery_target,
            "self_delivery_error": self.self_delivery_error,
        }


@dataclass(frozen=True)
class CollectedMergeRunResult:
    status: str
    target_date: str
    input_dir: str
    output_path: str | None
    source_file_count: int
    source_event_count: int
    merged_event_count: int
    skipped_file_count: int
    partial_file_count: int = 0
    quality_summary: CollectedMergeQualitySummary = field(
        default_factory=CollectedMergeQualitySummary
    )
    warning_messages: list[str] = field(default_factory=list)
    self_delivery_status: str = ""
    self_delivery_target: str = ""
    self_delivery_error: str = ""
    outputs: list[CollectedMergeOutput] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CollectedMergeRunResult:
        return cls(
            status=str(data["status"]),
            target_date=str(data["target_date"]),
            input_dir=str(data["input_dir"]),
            output_path=(
                None if data.get("output_path") is None else str(data["output_path"])
            ),
            source_file_count=int(data.get("source_file_count", 0)),
            source_event_count=int(data.get("source_event_count", 0)),
            merged_event_count=int(data.get("merged_event_count", 0)),
            skipped_file_count=int(data.get("skipped_file_count", 0)),
            partial_file_count=int(data.get("partial_file_count", 0)),
            quality_summary=CollectedMergeQualitySummary.from_dict(
                data.get("quality_summary", {})
                if isinstance(data.get("quality_summary", {}), dict)
                else {}
            ),
            warning_messages=_string_list(data.get("warning_messages")),
            self_delivery_status=str(
                data.get("self_delivery_status", data.get("upload_status", ""))
            ),
            self_delivery_target=str(
                data.get("self_delivery_target", data.get("upload_target", ""))
            ),
            self_delivery_error=str(
                data.get("self_delivery_error", data.get("upload_error", ""))
            ),
            outputs=[
                CollectedMergeOutput.from_dict(item)
                for item in _dict_list(data.get("outputs"))
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "target_date": self.target_date,
            "input_dir": self.input_dir,
            "output_path": self.output_path,
            "source_file_count": self.source_file_count,
            "source_event_count": self.source_event_count,
            "merged_event_count": self.merged_event_count,
            "skipped_file_count": self.skipped_file_count,
            "partial_file_count": self.partial_file_count,
            "quality_summary": self.quality_summary.to_dict(),
            "warning_messages": list(self.warning_messages),
            "self_delivery_status": self.self_delivery_status,
            "self_delivery_target": self.self_delivery_target,
            "self_delivery_error": self.self_delivery_error,
            "outputs": [item.to_dict() for item in self.outputs],
        }


@dataclass(frozen=True)
class PreflightResult:
    status: str
    error_summary: str
    details: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PreflightResult:
        details = data.get("details", {})
        if not isinstance(details, dict):
            raise TypeError("Expected details to be a dictionary.")
        normalized: dict[str, str] = {
            str(key): str(value)
            for key, value in details.items()
        }
        return cls(
            status=str(data["status"]),
            error_summary=str(data.get("error_summary", "")),
            details=normalized,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "error_summary": self.error_summary,
            "details": dict(self.details),
        }
