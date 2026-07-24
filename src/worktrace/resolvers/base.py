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

    def load_required_image_summaries(
        self,
        message: NormalizedMessage,
        attachment_ids: list[str],
    ) -> list[AttachmentTextBlock] | None:
        """Load image summaries that must be available before model analysis."""
        return self.load_attachment_text_if_needed(
            message,
            attachment_ids,
            "Required image context",
        )

    def drain_warning_messages(self) -> list[str]:
        return []

    @abstractmethod
    def load_link_text_if_needed(
        self,
        message: NormalizedMessage,
        link_ids: list[str],
        hint: str,
    ) -> list[LinkedFileTextBlock] | None:
        raise NotImplementedError
