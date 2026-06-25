from __future__ import annotations

from abc import ABC, abstractmethod

from ..constants import ContextDirection
from ..models import ConversationRef, NormalizedMessage, SelfIdentity


class ChatSource(ABC):
    @abstractmethod
    def get_self_identity(self) -> SelfIdentity:
        raise NotImplementedError

    @abstractmethod
    def list_target_conversations(
        self,
        target_date: str,
        self_identity: SelfIdentity,
    ) -> list[ConversationRef]:
        raise NotImplementedError

    @abstractmethod
    def fetch_conversation_messages(
        self,
        target_date: str,
        conversation_ids: list[str],
    ) -> list[NormalizedMessage]:
        raise NotImplementedError

    @abstractmethod
    def fetch_related_messages(
        self,
        conversation_id: str,
        target_message_ids: list[str],
        direction: ContextDirection | str,
        limit: int,
    ) -> list[NormalizedMessage]:
        raise NotImplementedError
