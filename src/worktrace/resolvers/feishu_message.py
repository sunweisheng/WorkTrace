from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import subprocess
from html import unescape
from typing import Any, Sequence
from urllib.parse import urlparse

from ..config import RuntimeConfig
from ..constants import LinkType
from ..models import AttachmentTextBlock, LinkMeta, NormalizedMessage
from ..utils.link_refs import classify_link_type, collect_message_links
from ..utils.text import clean_text, extract_urls
from .base import ContentResolver


@dataclass
class FeishuMessageContentResolver(ContentResolver):
    config: RuntimeConfig
    command_runner: Any | None = None
    _doc_title_cache: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.command_runner is None:
            self.command_runner = self._run_command

    def to_text(self, message: NormalizedMessage) -> str:
        parts = [clean_text(message.text)]

        for link in self.extract_links(message):
            if link.title:
                parts.append(f"{link.title}: {link.url}")
            else:
                parts.append(link.url)

        return "\n".join(part for part in parts if part).strip()

    def extract_links(self, message: NormalizedMessage) -> list[LinkMeta]:
        links: list[LinkMeta] = []
        for item in collect_message_links(message):
            title = item.title
            if not title:
                title = self._resolve_link_title(item.url)
            links.append(
                LinkMeta(
                    url=item.url,
                    title=title,
                    link_type=item.link_type,
                )
            )
        return links

    def _classify_link_type(self, url: str) -> str:
        return classify_link_type(url)

    def _resolve_link_title(self, url: str) -> str:
        if self._classify_link_type(url) != LinkType.FEISHU_DOC.value:
            return ""
        cache_key = self._doc_cache_key(url)
        if not cache_key:
            return ""
        cached = self._doc_title_cache.get(cache_key)
        if cached is not None:
            return cached

        title = self._fetch_doc_title(url)
        self._doc_title_cache[cache_key] = title
        return title

    def _doc_cache_key(self, url: str) -> str:
        parsed = urlparse(url)
        match = re.search(r"/(docx|wiki)/([^/?#]+)", parsed.path)
        if not match:
            return ""
        return f"{match.group(1)}:{match.group(2)}"

    def _fetch_doc_title(self, url: str) -> str:
        result = self.command_runner(
            (
                "lark-cli",
                "docs",
                "+fetch",
                "--api-version",
                "v2",
                "--as",
                "user",
                "--doc",
                url,
                "--scope",
                "full",
                "--detail",
                "simple",
                "--doc-format",
                "xml",
                "--json",
            )
        )
        if getattr(result, "returncode", 1) != 0:
            return ""
        try:
            payload = json.loads(getattr(result, "stdout", "") or "{}")
        except json.JSONDecodeError:
            return ""
        content = (
            payload.get("data", {})
            .get("document", {})
            .get("content", "")
        )
        if not isinstance(content, str):
            return ""
        match = re.search(r"<title>(.*?)</title>", content, flags=re.DOTALL)
        if not match:
            return ""
        return clean_text(unescape(match.group(1)))

    def _run_command(
        self,
        args: Sequence[str],
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            check=False,
        )

    def load_attachment_text_if_needed(
        self,
        message: NormalizedMessage,
        attachment_ids: list[str],
        hint: str,
    ) -> list[AttachmentTextBlock] | None:
        selected_ids = set(attachment_ids)
        blocks: list[AttachmentTextBlock] = []

        for attachment in message.attachments:
            if attachment.attachment_id not in selected_ids:
                continue
            blocks.append(
                AttachmentTextBlock(
                    attachment_id=attachment.attachment_id,
                    message_id=message.message_id,
                    file_name=attachment.file_name,
                    text=f"[Attachment placeholder] {attachment.file_name}\nHint: {hint}".strip(),
                )
            )

        return blocks or None
