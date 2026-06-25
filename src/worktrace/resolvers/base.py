from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import AttachmentTextBlock, NormalizedMessage


class ContentResolver(ABC):
    @abstractmethod
    def to_text(self, message: NormalizedMessage) -> str:
        raise NotImplementedError

    @abstractmethod
    def load_attachment_text_if_needed(
        self,
        message: NormalizedMessage,
        attachment_ids: list[str],
        hint: str,
    ) -> list[AttachmentTextBlock] | None:
        raise NotImplementedError
