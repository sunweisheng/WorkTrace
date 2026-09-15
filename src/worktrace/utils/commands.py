from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Mapping, Sequence


_WINDOWS_CMD_COMMANDS = frozenset({"codex", "lark-cli"})


def prepare_command_args(
    args: Sequence[str],
    *,
    os_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> list[str]:
    command = list(args)
    platform_name = os.name if os_name is None else os_name
    if (
        platform_name != "nt"
        or not command
        or command[0].casefold() not in _WINDOWS_CMD_COMMANDS
    ):
        return command

    launcher = which(f"{command[0]}.cmd")
    if launcher is None:
        raise FileNotFoundError(
            f"Could not find Windows command launcher: {command[0]}.cmd"
        )

    environment = os.environ if environ is None else environ
    comspec = next(
        (
            value
            for key, value in environment.items()
            if key.casefold() == "comspec" and value
        ),
        "cmd.exe",
    )
    command_line = subprocess.list2cmdline([launcher, *command[1:]])
    return [comspec, "/d", "/s", "/c", command_line]


def run_text_command(
    args: Sequence[str],
    *,
    cwd: Path | str | None = None,
    timeout: int | float | None = None,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    capture_output: bool = True,
    text: bool = True,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        prepare_command_args(args, environ=env),
        cwd=str(cwd) if cwd else None,
        capture_output=capture_output,
        text=text,
        encoding="utf-8",
        errors="replace",
        input=input_text,
        timeout=timeout,
        check=check,
        env=env,
    )
