from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..reaction_catalog import ReactionCatalog


@dataclass(frozen=True)
class ReactionCatalogSyncResult:
    source_id: str
    entry_count: int
    catalog_path: Path
    asset_dir: Path
    status: str = "ok"
    error_summary: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "entry_count": self.entry_count,
            "catalog_path": str(self.catalog_path),
            "asset_dir": str(self.asset_dir),
            "status": self.status,
            "error_summary": self.error_summary,
        }


class ReactionCatalogProvider(ABC):
    source_id: str

    @abstractmethod
    def synchronize(self) -> ReactionCatalogSyncResult:
        """Refresh this source's catalog and versioned image resources."""

    @abstractmethod
    def build_catalog(self) -> ReactionCatalog:
        """Fetch and validate a catalog without writing it to the repository."""
