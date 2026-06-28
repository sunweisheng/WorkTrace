from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..models import SelfIdentity


class DeliveryChannel(ABC):
    @abstractmethod
    def deliver_to_self(
        self,
        *,
        self_identity: SelfIdentity,
        markdown_path: Path,
    ) -> tuple[str, str]:
        raise NotImplementedError
