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
    links: list[LinkMeta] = field(default_factory=list)
    attachments: list[AttachmentMeta] = field(default_factory=list)
    is_system: bool = False

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
            links=[LinkMeta.from_dict(item) for item in _dict_list(data.get("links"))],
            attachments=[
                AttachmentMeta.from_dict(item)
                for item in _dict_list(data.get("attachments"))
            ],
            is_system=bool(data.get("is_system", False)),
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
            "links": [item.to_dict() for item in self.links],
            "attachments": [item.to_dict() for item in self.attachments],
            "is_system": self.is_system,
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
class ConversationSlice:
    slice_id: str
    conversation_id: str
    conversation_name: str
    anchor_message_ids: list[str]
    in_day_message_ids: list[str]
    messages: list[NormalizedMessage]
    attachment_texts: list[AttachmentTextBlock] = field(default_factory=list)

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
    reply_relation_ids: list[str] = field(default_factory=list)
    quote_relation_ids: list[str] = field(default_factory=list)
    attachment_refs: list[AttachmentMeta] = field(default_factory=list)

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
            reply_relation_ids=_string_list(data.get("reply_relation_ids")),
            quote_relation_ids=_string_list(data.get("quote_relation_ids")),
            attachment_refs=[
                AttachmentMeta.from_dict(item)
                for item in _dict_list(data.get("attachment_refs"))
            ],
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
            "reply_relation_ids": list(self.reply_relation_ids),
            "quote_relation_ids": list(self.quote_relation_ids),
            "attachment_refs": [item.to_dict() for item in self.attachment_refs],
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
    target_attachment_ids: list[str]
    reason: str
    limit: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContextRequest:
        return cls(
            slice_id=str(data["slice_id"]),
            request_type=str(data["request_type"]),
            target_message_ids=_string_list(data.get("target_message_ids")),
            target_attachment_ids=_string_list(data.get("target_attachment_ids")),
            reason=str(data.get("reason", "")),
            limit=int(data.get("limit", 0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "slice_id": self.slice_id,
            "request_type": self.request_type,
            "target_message_ids": list(self.target_message_ids),
            "target_attachment_ids": list(self.target_attachment_ids),
            "reason": self.reason,
            "limit": self.limit,
        }


def _parse_context_requests(value: Any) -> list[ContextRequest]:
    parsed: list[ContextRequest] = []
    for item in _dict_list(value):
        try:
            parsed.append(ContextRequest.from_dict(item))
        except (KeyError, TypeError, ValueError):
            continue
    return parsed


@dataclass(frozen=True)
class SourceBackedEventDraft:
    draft_id: str
    date: str
    topic: str
    content: str
    result: str
    source_message_ids: list[str]
    source_conversation_id: str
    source_slice_id: str
    confidence: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceBackedEventDraft:
        return cls(
            draft_id=str(data["draft_id"]),
            date=str(data["date"]),
            topic=str(data.get("topic", "")),
            content=str(data.get("content", "")),
            result=str(data.get("result", "")),
            source_message_ids=_string_list(data.get("source_message_ids")),
            source_conversation_id=str(data["source_conversation_id"]),
            source_slice_id=str(data["source_slice_id"]),
            confidence=float(data.get("confidence", 0.0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "date": self.date,
            "topic": self.topic,
            "content": self.content,
            "result": self.result,
            "source_message_ids": list(self.source_message_ids),
            "source_conversation_id": self.source_conversation_id,
            "source_slice_id": self.source_slice_id,
            "confidence": self.confidence,
        }


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
    result: str
    source_message_ids: list[str]
    source_conversation_ids: list[str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MergedEventDraft:
        return cls(
            date=str(data["date"]),
            topic=str(data.get("topic", "")),
            content=str(data.get("content", "")),
            result=str(data.get("result", "")),
            source_message_ids=_string_list(data.get("source_message_ids")),
            source_conversation_ids=_string_list(data.get("source_conversation_ids")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "topic": self.topic,
            "content": self.content,
            "result": self.result,
            "source_message_ids": list(self.source_message_ids),
            "source_conversation_ids": list(self.source_conversation_ids),
        }


@dataclass(frozen=True)
class CrossConversationGroup:
    group_id: str
    draft_ids: list[str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CrossConversationGroup:
        return cls(
            group_id=str(data["group_id"]),
            draft_ids=_string_list(data.get("draft_ids")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "draft_ids": list(self.draft_ids),
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
    topic: str
    content: str
    result: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkEvent:
        return cls(
            date=str(data["date"]),
            event_id=str(data["event_id"]),
            topic=str(data.get("topic", "")),
            content=str(data.get("content", "")),
            result=str(data.get("result", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "event_id": self.event_id,
            "topic": self.topic,
            "content": self.content,
            "result": self.result,
        }


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
            "needs_cross_anchor_merge": self.needs_cross_anchor_merge,
            "created_at": self.created_at,
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
        }
