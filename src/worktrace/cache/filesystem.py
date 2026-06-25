from __future__ import annotations

import shutil
from pathlib import Path

from ..models import AnchorCacheEntry
from ..utils.json_io import dump_json, load_json_object
from .base import AnchorCacheStore


class FileSystemAnchorCacheStore(AnchorCacheStore):
    def __init__(self, root: Path) -> None:
        self.root = root

    def read(
        self,
        *,
        target_date: str,
        anchor_unit_id: str,
        input_fingerprint: str,
    ) -> AnchorCacheEntry | None:
        path = self._entry_path(target_date, anchor_unit_id, input_fingerprint)
        if not path.exists():
            return None
        try:
            payload = load_json_object(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        try:
            return AnchorCacheEntry.from_dict(payload)
        except (KeyError, TypeError, ValueError):
            return None

    def write(self, entry: AnchorCacheEntry) -> None:
        path = self._entry_path(
            entry.target_date,
            entry.anchor_unit_id,
            entry.input_fingerprint,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_json(entry.to_dict(), pretty=True) + "\n", encoding="utf-8")

    def invalidate_day(self, target_date: str) -> int:
        directory = self._day_dir(target_date)
        if not directory.exists():
            return 0
        files = list(directory.rglob("*.json"))
        shutil.rmtree(directory)
        return len(files)

    def _entry_path(
        self,
        target_date: str,
        anchor_unit_id: str,
        input_fingerprint: str,
    ) -> Path:
        return self._day_dir(target_date) / anchor_unit_id / f"{input_fingerprint}.json"

    def _day_dir(self, target_date: str) -> Path:
        year, month, day = target_date.split("-")
        return self.root / "anchors" / year / month / f"{year}-{month}-{day}"
