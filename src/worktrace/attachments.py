from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import RuntimeConfig
from .utils.text import clean_text


@dataclass(frozen=True)
class AttachmentTextSettings:
    enabled: bool
    max_files_per_run: int
    max_file_bytes: int
    allowed_extensions: tuple[str, ...]

    @classmethod
    def load(
        cls,
        config: RuntimeConfig,
        *,
        cwd: Path | None = None,
    ) -> "AttachmentTextSettings":
        path = (cwd or Path.cwd()) / "config" / "attachment_text.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(False, 0, 0, ())
        if not isinstance(payload, dict):
            raise ValueError(f"Invalid attachment text config: {path} must contain an object.")

        enabled = payload.get("enabled", False)
        max_files = payload.get("max_files_per_run", 0)
        max_bytes = payload.get("max_file_bytes", 0)
        extensions = payload.get("allowed_extensions", [])
        if not isinstance(enabled, bool):
            raise ValueError(f"Invalid attachment text config: {path} has invalid enabled.")
        if not isinstance(max_files, int) or max_files < 0:
            raise ValueError(f"Invalid attachment text config: max_files_per_run must be non-negative.")
        if not isinstance(max_bytes, int) or max_bytes < 0:
            raise ValueError(f"Invalid attachment text config: max_file_bytes must be non-negative.")
        if not isinstance(extensions, list) or not all(isinstance(item, str) for item in extensions):
            raise ValueError(f"Invalid attachment text config: allowed_extensions must be a string list.")
        return cls(
            enabled=enabled,
            max_files_per_run=max_files,
            max_file_bytes=max_bytes,
            allowed_extensions=tuple(item.strip().lower() for item in extensions if item.strip()),
        )


class TextAttachmentExtractor:
    def __init__(
        self,
        *,
        config: RuntimeConfig,
        settings: AttachmentTextSettings | None = None,
    ) -> None:
        self.settings = settings or AttachmentTextSettings.load(config)
        self._count = 0

    def is_supported(self, file_name: str) -> bool:
        return (
            self.settings.enabled
            and self._count < self.settings.max_files_per_run
            and Path(file_name).suffix.lower() in self.settings.allowed_extensions
        )

    def extract(self, path: Path, *, file_name: str) -> str:
        if not self.is_supported(file_name):
            return ""
        if not path.is_file() or path.stat().st_size > self.settings.max_file_bytes:
            return ""
        self._count += 1
        return clean_text(path.read_text(encoding="utf-8", errors="replace"))
