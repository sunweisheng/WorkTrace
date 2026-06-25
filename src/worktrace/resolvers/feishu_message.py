from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from ..config import RuntimeConfig
from ..constants import LinkType
from ..models import AttachmentTextBlock, LinkMeta, NormalizedMessage
from ..utils.text import clean_text, extract_urls
from .base import ContentResolver


@dataclass
class FeishuMessageContentResolver(ContentResolver):
    config: RuntimeConfig

    def to_text(self, message: NormalizedMessage) -> str:
        parts = [clean_text(message.text)]

        for link in self.extract_links(message):
            if link.title:
                parts.append(f"{link.title}: {link.url}")
            else:
                parts.append(link.url)

        return "\n".join(part for part in parts if part).strip()

    def extract_links(self, message: NormalizedMessage) -> list[LinkMeta]:
        explicit = list(message.links)
        known_urls = {item.url for item in explicit}

        for url in extract_urls(message.text):
            if url in known_urls:
                continue
            explicit.append(
                LinkMeta(
                    url=url,
                    title="",
                    link_type=self._classify_link_type(url),
                )
            )
        return explicit

    def _classify_link_type(self, url: str) -> str:
        host = urlparse(url).netloc.lower()
        if "feishu" in host or "larksuite" in host:
            return LinkType.FEISHU_DOC.value
        return LinkType.NORMAL.value

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
