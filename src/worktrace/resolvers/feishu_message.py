from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import re
import subprocess
import tempfile
from threading import Lock
from html import unescape
from pathlib import Path
from xml.etree import ElementTree
from typing import Any, Sequence
from urllib.parse import urlparse

from ..attachments import TextAttachmentExtractor
from ..config import RuntimeConfig
from ..constants import LinkType
from ..models import AttachmentTextBlock, LinkMeta, LinkedFileTextBlock, NormalizedMessage
from ..utils.link_refs import build_message_link_candidates, classify_link_type, collect_message_links
from ..utils.text import clean_text, extract_urls
from ..vision import OnlineImageSummarizer
from .base import ContentResolver

logger = logging.getLogger("worktrace")


@dataclass
class FeishuMessageContentResolver(ContentResolver):
    config: RuntimeConfig
    command_runner: Any | None = None
    image_summarizer: OnlineImageSummarizer | None = None
    image_downloader: Any | None = None
    text_attachment_extractor: TextAttachmentExtractor | None = None
    attachment_downloader: Any | None = None
    _doc_title_cache: dict[str, str] = field(default_factory=dict)
    _doc_content_cache: dict[str, str] = field(default_factory=dict)
    _image_summary_cache: dict[tuple[str, str], AttachmentTextBlock] = field(
        default_factory=dict
    )
    _image_summary_lock: Lock = field(default_factory=Lock)

    def __post_init__(self) -> None:
        if self.command_runner is None:
            self.command_runner = self._run_command
        if self.text_attachment_extractor is None:
            self.text_attachment_extractor = TextAttachmentExtractor(config=self.config)

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

        title, _ = self._fetch_doc_content(url)
        self._doc_title_cache[cache_key] = title
        return title

    def _doc_cache_key(self, url: str) -> str:
        parsed = urlparse(url)
        match = re.search(r"/(docx|wiki)/([^/?#]+)", parsed.path)
        if not match:
            return ""
        return f"{match.group(1)}:{match.group(2)}"

    def _fetch_doc_content(self, url: str) -> tuple[str, str]:
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
            return "", ""
        try:
            payload = json.loads(getattr(result, "stdout", "") or "{}")
        except json.JSONDecodeError:
            return "", ""
        content = (
            payload.get("data", {})
            .get("document", {})
            .get("content", "")
        )
        if not isinstance(content, str):
            return "", ""
        title = self._extract_doc_title(content)
        return title, self._xml_to_text(content)

    def _extract_doc_title(self, content: str) -> str:
        match = re.search(r"<title>(.*?)</title>", content, flags=re.DOTALL)
        if not match:
            return ""
        return clean_text(unescape(match.group(1)))

    def _xml_to_text(self, content: str) -> str:
        try:
            root = ElementTree.fromstring(f"<root>{content}</root>")
            text = "".join(root.itertext())
        except ElementTree.ParseError:
            text = re.sub(r"<[^>]+>", "\n", content)
        return clean_text(unescape(text))

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
        if self.text_attachment_extractor is None:
            return None
        selected_ids = set(attachment_ids)
        blocks: list[AttachmentTextBlock] = []

        for attachment in message.attachments:
            if attachment.attachment_id not in selected_ids:
                continue
            if self._is_image_attachment(message, attachment):
                image_summary = self._load_image_summary_if_needed(message, attachment)
                if image_summary is not None:
                    blocks.append(image_summary)
                continue
            if not self.text_attachment_extractor.is_supported(attachment.file_name):
                continue
            try:
                text = self._load_text_attachment(message, attachment)
            except Exception as exc:
                logger.warning(
                    "Skipped attachment text for message %s: %s",
                    message.message_id,
                    exc,
                )
                continue
            if text:
                blocks.append(
                    AttachmentTextBlock(
                        attachment_id=attachment.attachment_id,
                        message_id=message.message_id,
                        file_name=attachment.file_name,
                        text=f"附件正文：{text}",
                    )
                )

        return blocks or None

    def load_required_image_summaries(
        self,
        message: NormalizedMessage,
        attachment_ids: list[str],
    ) -> list[AttachmentTextBlock] | None:
        selected_ids = set(attachment_ids)
        blocks: list[AttachmentTextBlock] = []
        for attachment in message.attachments:
            if (
                attachment.attachment_id not in selected_ids
                or not self._is_image_attachment(message, attachment)
            ):
                continue
            image_summary = self._load_image_summary_if_needed(
                message,
                attachment,
                required=True,
            )
            if image_summary is not None:
                blocks.append(image_summary)
        return blocks or None

    def _load_image_summary_if_needed(
        self,
        message: NormalizedMessage,
        attachment: Any,
        *,
        required: bool = False,
    ) -> AttachmentTextBlock | None:
        if self.image_summarizer is None:
            return None
        key = (message.message_id, attachment.attachment_id)
        with self._image_summary_lock:
            cached = self._image_summary_cache.get(key)
            if cached is not None:
                return cached
            try:
                summary = self._summarize_image_attachment(
                    message,
                    attachment.attachment_id,
                    required=required,
                )
            except Exception as exc:
                logger.warning("Skipped image summary for message %s: %s", message.message_id, exc)
                return None
            if not summary:
                return None
            block = AttachmentTextBlock(
                attachment_id=attachment.attachment_id,
                message_id=message.message_id,
                file_name=attachment.file_name,
                text=f"图片内容摘要：{clean_text(summary)}",
            )
            self._image_summary_cache[key] = block
            return block

    def _load_text_attachment(self, message: NormalizedMessage, attachment: Any) -> str:
        if self.text_attachment_extractor is None:
            return ""
        if self.attachment_downloader is not None:
            path = self.attachment_downloader(message, attachment.attachment_id)
            return self.text_attachment_extractor.extract(path, file_name=attachment.file_name)
        with tempfile.TemporaryDirectory(prefix="worktrace-attachment-") as temp_dir:
            result = subprocess.run(
                [
                    "lark-cli", "im", "+messages-resources-download", "--as", "user",
                    "--message-id", message.message_id, "--file-key", attachment.attachment_id,
                    "--type", "file", "--output", "attachment",
                ],
                cwd=temp_dir,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "attachment download failed")
            paths = [item for item in Path(temp_dir).iterdir() if item.is_file()]
            if not paths:
                raise RuntimeError("attachment download did not produce a file")
            return self.text_attachment_extractor.extract(paths[0], file_name=attachment.file_name)

    def _is_image_attachment(self, message: NormalizedMessage, attachment: Any) -> bool:
        return message.message_type in {"image", "media", "post"} or attachment.mime_type.startswith("image/")

    def _summarize_image_attachment(
        self,
        message: NormalizedMessage,
        attachment_id: str,
        *,
        required: bool = False,
    ) -> str:
        if self.image_downloader is not None:
            return self.image_summarizer.summarize(
                self.image_downloader(message, attachment_id),
                required=required,
            )
        with tempfile.TemporaryDirectory(prefix="worktrace-image-") as temp_dir:
            result = subprocess.run(
                [
                    "lark-cli", "im", "+messages-resources-download", "--as", "user",
                    "--message-id", message.message_id, "--file-key", attachment_id,
                    "--type", "image", "--output", "image",
                ],
                cwd=temp_dir,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "image download failed")
            paths = [item for item in Path(temp_dir).iterdir() if item.is_file()]
            if not paths:
                raise RuntimeError("image download did not produce a file")
            return self.image_summarizer.summarize(paths[0], required=required)

    def load_link_text_if_needed(
        self,
        message: NormalizedMessage,
        link_ids: list[str],
        hint: str,
    ) -> list[LinkedFileTextBlock] | None:
        selected_ids = set(link_ids)
        blocks: list[LinkedFileTextBlock] = []
        for link in build_message_link_candidates(message):
            if link.link_id not in selected_ids:
                continue
            if link.link_type != LinkType.FEISHU_DOC.value:
                continue
            cache_key = self._doc_cache_key(link.url)
            if not cache_key:
                continue
            cached_text = self._doc_content_cache.get(cache_key)
            cached_title = self._doc_title_cache.get(cache_key)
            if cached_text is None or cached_title is None:
                title, text = self._fetch_doc_content(link.url)
                self._doc_title_cache[cache_key] = title
                self._doc_content_cache[cache_key] = text
                cached_title = title
                cached_text = text
            if not cached_text:
                continue
            blocks.append(
                LinkedFileTextBlock(
                    link_id=link.link_id,
                    message_id=message.message_id,
                    title=clean_text(cached_title or link.title),
                    url=link.url,
                    text=f"{cached_text}\nHint: {hint}".strip(),
                )
            )
        return blocks or None
