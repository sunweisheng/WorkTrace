from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import AnchorCacheEntry


class AnchorCacheStore(ABC):
    @abstractmethod
    def read(
        self,
        *,
        target_date: str,
        anchor_unit_id: str,
        input_fingerprint: str,
    ) -> AnchorCacheEntry | None:
        raise NotImplementedError

    @abstractmethod
    def write(self, entry: AnchorCacheEntry) -> None:
        raise NotImplementedError

    @abstractmethod
    def invalidate_day(self, target_date: str) -> int:
        raise NotImplementedError
