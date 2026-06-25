from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import DayDocument, StoreWriteResult, WorkEvent


class EventStore(ABC):
    @abstractmethod
    def replace_day(
        self,
        target_date: str,
        events: list[WorkEvent],
    ) -> StoreWriteResult:
        raise NotImplementedError

    @abstractmethod
    def read_day(self, target_date: str) -> DayDocument | None:
        raise NotImplementedError
