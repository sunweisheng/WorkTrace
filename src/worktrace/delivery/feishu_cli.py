from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..errors import DeliveryError
from ..models import SelfIdentity
from .base import DeliveryChannel

_DELIVERY_COPY_DIRNAME = ".self_delivery"


@dataclass
class FeishuCliSelfDelivery(DeliveryChannel):
    command_runner: Any | None = None
    cwd: Path | None = None

    def __post_init__(self) -> None:
        if self.command_runner is None:
            self.command_runner = self._run_command
        if self.cwd is None:
            self.cwd = Path.cwd()

    def deliver_to_self(
        self,
        *,
        self_identity: SelfIdentity,
        markdown_path: Path,
    ) -> tuple[str, str]:
        delivery_path = self._prepare_delivery_copy(
            markdown_path=markdown_path,
            self_identity=self_identity,
        )
        try:
            relative_path = delivery_path.relative_to(self.cwd)
            result = self.command_runner(
                (
                    "lark-cli",
                    "im",
                    "+messages-send",
                    "--as",
                    "bot",
                    "--user-id",
                    self_identity.open_id,
                    "--file",
                    str(relative_path),
                ),
                cwd=self.cwd,
            )
            if getattr(result, "returncode", 1) != 0:
                stderr = (getattr(result, "stderr", "") or "").strip()
                raise DeliveryError(stderr or "Failed to deliver markdown to self.")
            return ("success", self_identity.open_id)
        finally:
            self._cleanup_delivery_copy(markdown_path=markdown_path, delivery_path=delivery_path)

    def _run_command(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(args),
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
        )

    def _prepare_delivery_copy(
        self,
        *,
        markdown_path: Path,
        self_identity: SelfIdentity,
    ) -> Path:
        target_name = self._build_delivery_filename(
            markdown_path=markdown_path,
            self_identity=self_identity,
        )
        if markdown_path.name == target_name:
            return markdown_path

        delivery_dir = markdown_path.parent / _DELIVERY_COPY_DIRNAME
        delivery_dir.mkdir(parents=True, exist_ok=True)
        delivery_path = delivery_dir / target_name
        try:
            shutil.copyfile(markdown_path, delivery_path)
        except OSError as exc:
            raise DeliveryError("Failed to prepare markdown file for self delivery.") from exc
        return delivery_path

    def _cleanup_delivery_copy(self, *, markdown_path: Path, delivery_path: Path) -> None:
        if delivery_path == markdown_path:
            return
        try:
            delivery_path.unlink(missing_ok=True)
        except OSError:
            return
        try:
            delivery_path.parent.rmdir()
        except OSError:
            return

    def _build_delivery_filename(
        self,
        *,
        markdown_path: Path,
        self_identity: SelfIdentity,
    ) -> str:
        date_part = markdown_path.stem.strip() or "worktrace"
        display_name = self._sanitize_filename_component(self_identity.display_name)
        if not display_name:
            display_name = self._sanitize_filename_component(self_identity.open_id) or "self"
        suffix = markdown_path.suffix or ".md"
        return f"{date_part}-{display_name}{suffix}"

    def _sanitize_filename_component(self, value: str) -> str:
        sanitized = re.sub(r'[\\/:*?"<>|\r\n]+', "_", value.strip())
        sanitized = re.sub(r"\s+", "_", sanitized)
        return sanitized.strip(" ._")
