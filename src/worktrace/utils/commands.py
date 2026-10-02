from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Mapping, Sequence

import mslex


_WINDOWS_LAUNCHER_COMMANDS = frozenset({"codex", "lark-cli"})
_WINDOWS_SHELL_LAUNCHER_SUFFIXES = frozenset({".bat", ".cmd"})


def _protect_batch_forwarding(command_line: str) -> str:
    """Preserve mslex escapes through CMD before a batch shim forwards %*."""
    result: list[str] = []
    quoted = False
    index = 0
    while index < len(command_line):
        char = command_line[index]
        if char == '"':
            quoted = not quoted
        if not quoted and char == "^" and index + 1 < len(command_line):
            next_char = command_line[index + 1]
            result.append("^^^^" if next_char == "^" else "^^^" + next_char)
            index += 2
        else:
            result.append(char)
            index += 1
    return "".join(result)


def prepare_command_args(
    args: Sequence[str],
    *,
    os_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> list[str] | str:
    command = list(args)
    platform_name = os.name if os_name is None else os_name
    if (
        platform_name != "nt"
        or not command
        or command[0].casefold() not in _WINDOWS_LAUNCHER_COMMANDS
    ):
        return command

    launcher = next(
        (
            resolved
            for candidate in (command[0], f"{command[0]}.cmd", f"{command[0]}.exe")
            if (resolved := which(candidate)) is not None
        ),
        None,
    )
    if launcher is None:
        raise FileNotFoundError(
            f"Could not find Windows command launcher: {command[0]}.cmd or "
            f"{command[0]}.exe"
        )

    if Path(launcher).suffix.casefold() not in _WINDOWS_SHELL_LAUNCHER_SUFFIXES:
        return [launcher, *command[1:]]

    environment = os.environ if environ is None else environ
    comspec = next(
        (
            value
            for key, value in environment.items()
            if key.casefold() == "comspec" and value
        ),
        "cmd.exe",
    )
    command_line = _protect_batch_forwarding(
        mslex.join([launcher, *command[1:]], for_cmd=True)
    )
    # CMD /s removes the outer quote pair. Pass the complete command line
    # directly so subprocess does not escape its inner quotes a second time.
    return f'{subprocess.list2cmdline([comspec])} /d /s /v:off /c "{command_line}"'


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
