from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..errors import DeliveryError
from ..models import SelfIdentity
from .base import DeliveryChannel


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
        relative_path = markdown_path.relative_to(self.cwd)
        result = self.command_runner(
            (
                "lark-cli",
                "im",
                "+messages-send",
                "--as",
                "user",
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
