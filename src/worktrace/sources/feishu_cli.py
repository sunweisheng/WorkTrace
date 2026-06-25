from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
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


@dataclass
class FeishuCliChatSource(ChatSource):
    config: RuntimeConfig
    command_runner: Any | None = None

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
        start, end = day_bounds(target_date, self.config.timezone)
        messages: list[NormalizedMessage] = []
        page_count = 0

        for conversation_id in conversation_ids:
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
            conversation_count=len(conversation_ids),
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
                "links": links,
                "attachments": attachments,
                "is_system": is_system,
            }
        )

    def _parse_content(self, content: Any) -> dict[str, Any]:
        if isinstance(content, str):
            try:
                loaded = json.loads(content)
            except json.JSONDecodeError:
                return {"text": content, "links": [], "attachments": []}
            return self._parse_content(loaded)

        if not isinstance(content, dict):
            return {"text": "", "links": [], "attachments": []}

        text_parts: list[str] = []
        links: list[dict[str, Any]] = []
        attachments: list[dict[str, Any]] = []

        for key in ("text", "title", "content", "summary"):
            value = content.get(key)
            if isinstance(value, str):
                text_parts.append(value)

        if isinstance(content.get("lines"), list):
            for line in content["lines"]:
                if not isinstance(line, list):
                    continue
                for block in line:
                    if not isinstance(block, dict):
                        continue
                    if isinstance(block.get("text"), str):
                        text_parts.append(block["text"])
                    href = block.get("href") or block.get("url")
                    if href:
                        links.append(
                            {
                                "url": str(href),
                                "title": str(block.get("text") or block.get("title") or ""),
                                "link_type": "feishu_doc"
                                if "feishu" in str(href) or "larksuite" in str(href)
                                else "normal",
                            }
                        )

        if isinstance(content.get("attachments"), list):
            for item in content["attachments"]:
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

        attachments.extend(self._extract_media_attachments(content))

        return {
            "text": clean_text("\n".join(part for part in text_parts if part)),
            "links": [item for item in links if item.get("url")],
            "attachments": self._dedupe_attachment_dicts(
                [item for item in attachments if item.get("attachment_id")]
            ),
        }

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

    def _dedupe_and_sort(self, messages: list[NormalizedMessage]) -> list[NormalizedMessage]:
        deduped: dict[str, NormalizedMessage] = {}
        for message in messages:
            if not message.message_id:
                continue
            deduped[message.message_id] = message
        return sorted(deduped.values(), key=lambda item: (item.send_time, item.message_id))
