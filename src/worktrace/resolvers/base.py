from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import (
    AttachmentTextBlock,
    LinkMeta,
    LinkedFileTextBlock,
    NormalizedMessage,
)


class ContentResolver(ABC):
    @abstractmethod
    def to_text(self, message: NormalizedMessage) -> str:
        raise NotImplementedError

    @abstractmethod
    def extract_links(self, message: NormalizedMessage) -> list[LinkMeta]:
        raise NotImplementedError

    @abstractmethod
    def load_attachment_text_if_needed(
        self,
        message: NormalizedMessage,
        attachment_ids: list[str],
        hint: str,
    ) -> list[AttachmentTextBlock] | None:
        raise NotImplementedError

    @abstractmethod
    def load_link_text_if_needed(
        self,
        message: NormalizedMessage,
        link_ids: list[str],
        hint: str,
    ) -> list[LinkedFileTextBlock] | None:
        raise NotImplementedError

    def summarize_images(self, messages: list[NormalizedMessage]) -> list[AttachmentTextBlock]:
        """Return transient LLM summaries for image attachments when supported."""
        return []
