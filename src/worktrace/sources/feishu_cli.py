from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any, Sequence

from ..constants import ContextDirection
from ..config import RuntimeConfig
from ..errors import ChatSourceError
from ..logging_utils import log_timing
from ..models import ConversationRef, NormalizedMessage, SelfIdentity
from ..utils.dates import day_bounds, is_same_target_date, normalize_datetime_string
from ..utils.text import clean_text
from .base import ChatSource

logger = logging.getLogger("worktrace")
_INLINE_IMAGE_KEY_RE = re.compile(r"\[Image:\s*(img_[^\]\s]+)\]", re.IGNORECASE)
_MARKDOWN_IMAGE_KEY_RE = re.compile(r"!\[[^\]]*\]\((img_[^)\s]+)\)", re.IGNORECASE)


@dataclass
class FeishuCliChatSource(ChatSource):
    source_id = "feishu"
    config: RuntimeConfig
    command_runner: Any | None = None
    conversation_modes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.command_runner is None:
            self.command_runner = self._run_command

    def get_self_identity(self) -> SelfIdentity:
        started_at = perf_counter()
        payload = self._run_json(("lark-cli", "auth", "status"))
        user = payload.get("identities", {}).get("user", {})

        open_id = user.get("openId")
        display_name = user.get("userName", "")
        if payload.get("identity") != "user" or not open_id:
            raise ChatSourceError("Failed to resolve current Feishu user identity.")

        identity = SelfIdentity(
            open_id=str(open_id),
            display_name=str(display_name or open_id),
            source="lark-cli",
        )
        log_timing(
            logger,
            "chat_source.get_self_identity",
            started_at,
            open_id=identity.open_id,
        )
        return identity

    def list_target_conversations(
        self,
        target_date: str,
        self_identity: SelfIdentity,
    ) -> list[ConversationRef]:
        started_at = perf_counter()
        excluded_conversation_ids = set(self.config.excluded_conversation_ids)
        start, end = day_bounds(target_date, self.config.timezone)
        payload = self._run_json(
            (
                "lark-cli",
                "im",
                "+messages-search",
                "--as",
                "user",
                "--sender",
                self_identity.open_id,
                "--start",
                start.isoformat(),
                "--end",
                end.isoformat(),
                "--page-all",
            )
        )

        conversations: dict[str, ConversationRef] = {}
        for item in self._extract_items(payload):
            message = self._normalize_message(item)
            if message.sender_open_id != self_identity.open_id:
                continue
            if not is_same_target_date(message.send_time, target_date, self.config.timezone):
                continue
            if message.conversation_id in excluded_conversation_ids:
                continue
            self._remember_conversation_mode(message)
            conversations.setdefault(
                message.conversation_id,
                ConversationRef(
                    conversation_id=message.conversation_id,
                    conversation_name=message.conversation_name,
                ),
            )

        reaction_payload = self._run_json(
            (
                "lark-cli",
                "im",
                "+messages-search",
                "--as",
                "user",
                "--start",
                start.isoformat(),
                "--end",
                end.isoformat(),
                "--page-all",
                "--page-limit",
                str(self.config.reaction_discovery_page_limit),
            )
        )
        for item in self._extract_items(reaction_payload):
            message = self._normalize_message(item)
            if message.conversation_id in excluded_conversation_ids:
                continue
            if not any(
                reaction.operator_open_id == self_identity.open_id
                and is_same_target_date(
                    reaction.action_time or message.send_time,
                    target_date,
                    self.config.timezone,
                )
                for reaction in message.reactions
            ):
                continue
            self._remember_conversation_mode(message)
            conversations.setdefault(
                message.conversation_id,
                ConversationRef(
                    conversation_id=message.conversation_id,
                    conversation_name=message.conversation_name,
                ),
            )

        results = sorted(conversations.values(), key=lambda item: item.conversation_id)
        log_timing(
            logger,
            "chat_source.list_target_conversations",
            started_at,
            target_date=target_date,
            conversation_count=len(results),
        )
        return results

    def fetch_conversation_messages(
        self,
        target_date: str,
        conversation_ids: list[str],
    ) -> list[NormalizedMessage]:
        started_at = perf_counter()
        excluded_conversation_ids = set(self.config.excluded_conversation_ids)
        filtered_conversation_ids = [
            conversation_id
            for conversation_id in conversation_ids
            if conversation_id not in excluded_conversation_ids
        ]
        start, end = day_bounds(target_date, self.config.timezone)
        messages: list[NormalizedMessage] = []
        page_count = 0

        for conversation_id in filtered_conversation_ids:
            page_token = ""
            while True:
                page_count += 1
                args = [
                    "lark-cli",
                    "im",
                    "+chat-messages-list",
                    "--as",
                    "user",
                    "--chat-id",
                    conversation_id,
                    "--order",
                    "asc",
                    "--page-size",
                    "50",
                    "--start",
                    start.isoformat(),
                    "--end",
                    end.isoformat(),
                ]
                if page_token:
                    args.extend(["--page-token", page_token])

                payload = self._run_json(tuple(args))
                for item in self._extract_items(payload):
                    normalized = self._normalize_message(item)
                    if is_same_target_date(
                        normalized.send_time,
                        target_date,
                        self.config.timezone,
                    ):
                        messages.append(normalized)

                page_token = self._next_page_token(payload)
                if not page_token:
                    break

        results = self._dedupe_and_sort(messages)
        log_timing(
            logger,
            "chat_source.fetch_conversation_messages",
            started_at,
            target_date=target_date,
            conversation_count=len(filtered_conversation_ids),
            page_count=page_count,
            message_count=len(results),
        )
        return results

    def fetch_related_messages(
        self,
        conversation_id: str,
        target_message_ids: list[str],
        direction: ContextDirection | str,
        limit: int,
    ) -> list[NormalizedMessage]:
        target_message_ids = [message_id.strip() for message_id in target_message_ids if message_id.strip()]
        if not target_message_ids:
            return []

        started_at = perf_counter()
        boundary_payload = self._run_json(
            (
                "lark-cli",
                "im",
                "+messages-mget",
                "--as",
                "user",
                "--message-ids",
                ",".join(target_message_ids),
            )
        )
        boundary_messages = self._dedupe_and_sort(
            [self._normalize_message(item) for item in self._extract_items(boundary_payload)]
        )
        if not boundary_messages:
            return []

        if str(direction) == ContextDirection.EARLIER.value:
            boundary_time = boundary_messages[0].send_time
            payload = self._run_json(
                (
                    "lark-cli",
                    "im",
                    "+chat-messages-list",
                    "--as",
                    "user",
                    "--chat-id",
                    conversation_id,
                    "--order",
                    "desc",
                    "--page-size",
                    str(max(limit, 1)),
                    "--end",
                    boundary_time,
                )
            )
        else:
            boundary_time = boundary_messages[-1].send_time
            payload = self._run_json(
                (
                    "lark-cli",
                    "im",
                    "+chat-messages-list",
                    "--as",
                    "user",
                    "--chat-id",
                    conversation_id,
                    "--order",
                    "asc",
                    "--page-size",
                    str(max(limit, 1)),
                    "--start",
                    boundary_time,
                )
            )

        excluded = set(target_message_ids)
        results = [
            item
            for item in self._dedupe_and_sort(
                [self._normalize_message(raw) for raw in self._extract_items(payload)]
            )
            if item.message_id not in excluded
        ]
        if str(direction) == ContextDirection.EARLIER.value:
            results = results[-limit:]
        else:
            results = results[:limit]
        log_timing(
            logger,
            "chat_source.fetch_related_messages",
            started_at,
            conversation_id=conversation_id,
            direction=str(direction),
            target_message_count=len(target_message_ids),
            result_count=len(results),
            limit=limit,
        )
        return results

    def fetch_messages_by_ids(
        self,
        conversation_id: str,
        message_ids: list[str],
    ) -> list[NormalizedMessage]:
        cleaned_ids = list(dict.fromkeys(item.strip() for item in message_ids if item.strip()))
        if not cleaned_ids:
            return []
        started_at = perf_counter()
        raw_messages: list[dict[str, Any]] = []
        for start in range(0, len(cleaned_ids), 50):
            payload = self._run_json(
                (
                    "lark-cli",
                    "im",
                    "+messages-mget",
                    "--as",
                    "user",
                    "--message-ids",
                    ",".join(cleaned_ids[start : start + 50]),
                )
            )
            raw_messages.extend(self._extract_items(payload))
        results = [
            message
            for message in self._dedupe_and_sort(
                [self._normalize_message(item) for item in raw_messages]
            )
            if message.conversation_id == conversation_id
        ]
        log_timing(
            logger,
            "chat_source.fetch_messages_by_ids",
            started_at,
            conversation_id=conversation_id,
            requested_count=len(cleaned_ids),
            result_count=len(results),
        )
        return results

    def _run_command(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: int | float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(args),
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def _run_json(self, args: Sequence[str]) -> dict[str, Any]:
        started_at = perf_counter()
        result = self.command_runner(args)
        log_timing(
            logger,
            "lark_cli.command.completed",
            started_at,
            command=" ".join(args[:3]),
            returncode=getattr(result, "returncode", None),
        )
        returncode = getattr(result, "returncode", 0)
        stdout = getattr(result, "stdout", "")
        stderr = getattr(result, "stderr", "")
        if returncode != 0:
            raise ChatSourceError(f"lark-cli command failed: {' '.join(args)}\n{stderr}".strip())
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ChatSourceError(f"lark-cli returned invalid JSON for: {' '.join(args)}") from exc
        if not isinstance(payload, dict):
            raise ChatSourceError("lark-cli JSON payload must be an object.")
        return payload

    def _extract_items(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        data = payload.get("data")
        candidates = [
            payload.get("items"),
            payload.get("messages"),
            data.get("items") if isinstance(data, dict) else None,
            data.get("messages") if isinstance(data, dict) else None,
            data.get("message_list") if isinstance(data, dict) else None,
        ]
        for candidate in candidates:
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
        return []

    def _next_page_token(self, payload: dict[str, Any]) -> str:
        data = payload.get("data")
        candidates = [
            payload.get("page_token"),
            data.get("page_token") if isinstance(data, dict) else None,
            payload.get("pageToken"),
            data.get("pageToken") if isinstance(data, dict) else None,
        ]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate:
                return candidate
        return ""

    def _normalize_message(self, raw: dict[str, Any]) -> NormalizedMessage:
        conversation_id = (
            raw.get("chat_id")
            or raw.get("conversation_id")
            or raw.get("chatId")
            or raw.get("conversationId")
            or raw.get("chat", {}).get("id")
        )
        conversation_name = (
            raw.get("chat_name")
            or raw.get("conversation_name")
            or raw.get("chatName")
            or raw.get("conversationName")
            or raw.get("chat", {}).get("name")
            or ""
        )
        message_id = raw.get("message_id") or raw.get("messageId") or raw.get("id")
        sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
        sender_id_raw = sender.get("id")
        sender_id = sender_id_raw if isinstance(sender_id_raw, dict) else {}
        sender_open_id = (
            raw.get("sender_open_id")
            or sender.get("open_id")
            or (
                sender_id_raw
                if sender.get("id_type") == "open_id" and isinstance(sender_id_raw, str)
                else None
            )
            or sender_id.get("open_id")
            or sender_id.get("openId")
        )
        sender_name = (
            raw.get("sender_name")
            or sender.get("name")
            or sender.get("sender_name")
            or ""
        )
        send_time = (
            raw.get("send_time")
            or raw.get("create_time")
            or raw.get("createTime")
            or raw.get("timestamp")
        )
        message_type = raw.get("message_type") or raw.get("msg_type") or raw.get("type") or "unknown"

        body = raw.get("body") if isinstance(raw.get("body"), dict) else {}
        content = body.get("content") or raw.get("content") or raw.get("text") or ""
        parsed_content = self._parse_content(content)
        text = clean_text(
            raw.get("text_content")
            or raw.get("text")
            or parsed_content.get("text")
            or body.get("text")
            or ""
        )
        links = parsed_content.get("links", [])
        attachments = parsed_content.get("attachments", [])
        attachments.extend(self._extract_inline_image_attachments(text))
        mentioned_open_ids = self._dedupe_strings(
            [
                *parsed_content.get("mentioned_open_ids", []),
                *self._extract_mentioned_open_ids(raw.get("mentions")),
            ]
        )
        reactions = self._parse_reactions(raw.get("reactions"))
        extra_attachments = raw.get("attachments")
        if isinstance(extra_attachments, list):
            attachments.extend(
                [
                    {
                        "attachment_id": item.get("file_key")
                        or item.get("attachment_id")
                        or item.get("fileKey")
                        or item.get("id"),
                        "file_name": item.get("file_name") or item.get("name") or "",
                        "mime_type": item.get("mime_type") or item.get("mimeType") or "",
                        "file_size": item.get("file_size") or item.get("size"),
                    }
                    for item in extra_attachments
                    if isinstance(item, dict)
                ]
            )
        attachments = self._dedupe_attachment_dicts(attachments)

        is_system = bool(
            raw.get("is_system")
            or raw.get("system")
            or message_type in {"system", "recall", "chat_info"}
        )

        return NormalizedMessage.from_dict(
            {
                "conversation_id": str(conversation_id or ""),
                "conversation_name": str(conversation_name or ""),
                "message_id": str(message_id or ""),
                "sender_open_id": None if sender_open_id is None else str(sender_open_id),
                "sender_name": str(sender_name or ""),
                "send_time": normalize_datetime_string(send_time, self.config.timezone),
                "message_type": str(message_type),
                "text": text,
                "reply_to_message_id": raw.get("parent_id")
                or raw.get("reply_to")
                or raw.get("reply_to_message_id")
                or raw.get("replyToMessageId"),
                "quote_message_id": raw.get("root_id")
                or raw.get("quote_message_id")
                or raw.get("quoteMessageId"),
                "conversation_mode": self.conversation_modes.get(
                    str(conversation_id or ""),
                    self._conversation_mode_from_raw(raw),
                ),
                "links": links,
                "attachments": attachments,
                "is_system": is_system,
                "mentioned_open_ids": mentioned_open_ids,
                "reactions": reactions,
            }
        )

    def _remember_conversation_mode(self, message: NormalizedMessage) -> None:
        if message.conversation_mode:
            self.conversation_modes[message.conversation_id] = message.conversation_mode

    @staticmethod
    def _conversation_mode_from_raw(raw: dict[str, Any]) -> str:
        raw_mode = raw.get("chat_mode") or raw.get("chat_type") or ""
        mode = str(raw_mode).strip().lower()
        return mode if mode in {"p2p", "group", "topic"} else ""

    def _parse_content(self, content: Any) -> dict[str, Any]:
        if isinstance(content, str):
            try:
                loaded = json.loads(content)
            except json.JSONDecodeError:
                return {
                    "text": content,
                    "links": [],
                    "attachments": self._extract_inline_image_attachments(content),
                    "mentioned_open_ids": [],
                }
            return self._parse_content(loaded)

        if not isinstance(content, dict):
            return {
                "text": "",
                "links": [],
                "attachments": [],
                "mentioned_open_ids": [],
            }

        text_parts: list[str] = []
        links: list[dict[str, Any]] = []
        attachments: list[dict[str, Any]] = []
        mentioned_open_ids: list[str] = []
        self._collect_content_parts(
            content,
            text_parts,
            links,
            attachments,
            mentioned_open_ids,
        )

        return {
            "text": clean_text("\n".join(part for part in text_parts if part)),
            "links": self._dedupe_link_dicts([item for item in links if item.get("url")]),
            "attachments": self._dedupe_attachment_dicts(
                [item for item in attachments if item.get("attachment_id")]
            ),
            "mentioned_open_ids": self._dedupe_strings(mentioned_open_ids),
        }

    def _collect_content_parts(
        self,
        node: Any,
        text_parts: list[str],
        links: list[dict[str, Any]],
        attachments: list[dict[str, Any]],
        mentioned_open_ids: list[str],
    ) -> None:
        if isinstance(node, list):
            for item in node:
                self._collect_content_parts(
                    item,
                    text_parts,
                    links,
                    attachments,
                    mentioned_open_ids,
                )
            return

        if not isinstance(node, dict):
            return

        node_type = str(node.get("tag") or node.get("type") or "").lower()
        if node_type in {"at", "mention", "user"}:
            mentioned_open_ids.extend(self._extract_mentioned_open_ids(node))

        href = node.get("href") or node.get("url")
        if href:
            label = str(node.get("text") or node.get("title") or "").strip()
            if label:
                text_parts.append(label)
            links.append(
                {
                    "url": str(href),
                    "title": label,
                    "link_type": "feishu_doc"
                    if "feishu" in str(href) or "larksuite" in str(href)
                    else "normal",
                }
            )

        for key in ("text", "title", "summary"):
            value = node.get(key)
            if isinstance(value, str) and value and not href:
                text_parts.append(value)

        content_value = node.get("content")
        if isinstance(content_value, str):
            text_parts.append(content_value)

        if isinstance(node.get("attachments"), list):
            for item in node["attachments"]:
                if not isinstance(item, dict):
                    continue
                attachments.append(
                    {
                        "attachment_id": item.get("file_key")
                        or item.get("attachment_id")
                        or item.get("id"),
                        "file_name": item.get("file_name") or item.get("name") or "",
                        "mime_type": item.get("mime_type") or item.get("mimeType") or "",
                        "file_size": item.get("file_size") or item.get("size"),
                    }
                )

        attachments.extend(self._extract_media_attachments(node))

        for key, value in node.items():
            if key in {
                "text",
                "title",
                "summary",
                "href",
                "url",
                "attachments",
                "file_key",
                "image_key",
                "audio_key",
                "video_key",
                "file_name",
                "name",
                "mime_type",
                "mimeType",
                "file_size",
                "size",
            }:
                continue
            if isinstance(value, (dict, list)):
                self._collect_content_parts(
                    value,
                    text_parts,
                    links,
                    attachments,
                    mentioned_open_ids,
                )

    def _extract_mentioned_open_ids(self, value: Any) -> list[str]:
        if isinstance(value, list):
            values: list[str] = []
            for item in value:
                values.extend(self._extract_mentioned_open_ids(item))
            return values
        if not isinstance(value, dict):
            return []

        candidates = [
            value.get("open_id"),
            value.get("openId"),
            value.get("user_id"),
            value.get("userId"),
            value.get("id"),
        ]
        user = value.get("user")
        if isinstance(user, dict):
            candidates.extend(
                [
                    user.get("open_id"),
                    user.get("openId"),
                    user.get("user_id"),
                    user.get("userId"),
                    user.get("id"),
                ]
            )
        return [str(item) for item in candidates if isinstance(item, str) and item]

    def _parse_reactions(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, (dict, list)):
            return []

        records: list[dict[str, Any]] = []
        pending: list[tuple[Any, dict[str, Any]]] = [(value, {})]
        while pending:
            current, inherited = pending.pop()
            if isinstance(current, list):
                pending.extend((item, inherited) for item in current)
                continue
            if not isinstance(current, dict):
                continue
            reaction_type = current.get("reaction_type")
            metadata = {
                "reaction_id": current.get("reaction_id")
                or current.get("message_reaction_id")
                or inherited.get("reaction_id")
                or "",
                "emoji_type": current.get("emoji_type")
                or (
                    reaction_type.get("emoji_type")
                    if isinstance(reaction_type, dict)
                    else ""
                )
                or inherited.get("emoji_type")
                or "",
                "action_time": current.get("action_time")
                or current.get("create_time")
                or inherited.get("action_time")
                or "",
            }
            child_items = [
                current.get("message_reaction_items"),
                current.get("details"),
                current.get("items"),
            ]
            for items in child_items:
                if isinstance(items, list):
                    pending.extend((item, metadata) for item in items)

            operator = current.get("operator")
            if operator or current.get("operator_open_id") or current.get("operator_id"):
                operator_id = (
                    current.get("operator_open_id")
                    or current.get("operator_id")
                    or (operator.get("operator_id") if isinstance(operator, dict) else "")
                    or (operator.get("open_id") if isinstance(operator, dict) else "")
                    or ""
                )
                if metadata["reaction_id"] or metadata["emoji_type"]:
                    records.append(
                        {
                            "reaction_id": metadata["reaction_id"],
                            "operator_open_id": operator_id,
                            "emoji_type": metadata["emoji_type"],
                            "action_time": metadata["action_time"],
                        }
                    )

        seen: set[tuple[str, str, str, str]] = set()
        normalized: list[dict[str, Any]] = []
        for item in records:
            key = (
                str(item["reaction_id"]),
                str(item["operator_open_id"]),
                str(item["emoji_type"]),
                str(item["action_time"]),
            )
            if key in seen:
                continue
            seen.add(key)
            normalized.append(item)
        return normalized

    def _dedupe_strings(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            cleaned = str(value).strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                result.append(cleaned)
        return result

    def _extract_media_attachments(self, content: dict[str, Any]) -> list[dict[str, Any]]:
        media_specs = [
            ("image_key", "[Image]", "image/*"),
            ("file_key", "[File]", "application/octet-stream"),
            ("audio_key", "[Audio]", "audio/*"),
            ("video_key", "[Video]", "video/*"),
        ]
        attachments: list[dict[str, Any]] = []
        for key, default_name, mime_type in media_specs:
            value = content.get(key)
            if not isinstance(value, str) or not value:
                continue
            attachments.append(
                {
                    "attachment_id": value,
                    "file_name": str(content.get("file_name") or content.get("name") or default_name),
                    "mime_type": str(content.get("mime_type") or content.get("mimeType") or mime_type),
                    "file_size": content.get("file_size") or content.get("size"),
                }
            )
        return attachments

    def _extract_inline_image_attachments(self, content: str) -> list[dict[str, Any]]:
        keys = [
            *(_INLINE_IMAGE_KEY_RE.findall(content)),
            *(_MARKDOWN_IMAGE_KEY_RE.findall(content)),
        ]
        return [
            {
                "attachment_id": key,
                "file_name": "[Image]",
                "mime_type": "image/*",
                "file_size": None,
            }
            for key in self._dedupe_strings(keys)
        ]

    def _dedupe_attachment_dicts(
        self,
        attachments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        deduped: dict[str, dict[str, Any]] = {}
        for item in attachments:
            attachment_id = item.get("attachment_id")
            if not isinstance(attachment_id, str) or not attachment_id:
                continue
            deduped[attachment_id] = item
        return list(deduped.values())

    def _dedupe_link_dicts(self, links: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: dict[str, dict[str, Any]] = {}
        for item in links:
            url = item.get("url")
            if not isinstance(url, str) or not url:
                continue
            existing = deduped.get(url)
            if existing is None:
                deduped[url] = item
                continue
            existing_title = str(existing.get("title") or "").strip()
            candidate_title = str(item.get("title") or "").strip()
            if not existing_title and candidate_title:
                deduped[url] = item
        return list(deduped.values())

    def _dedupe_and_sort(self, messages: list[NormalizedMessage]) -> list[NormalizedMessage]:
        deduped: dict[str, NormalizedMessage] = {}
        for message in messages:
            if not message.message_id:
                continue
            deduped[message.message_id] = message
        return sorted(deduped.values(), key=lambda item: (item.send_time, item.message_id))
