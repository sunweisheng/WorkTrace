from __future__ import annotations

import subprocess
import sys

import pytest

from src.worktrace.utils.commands import prepare_command_args, run_text_command


def test_non_windows_command_is_unchanged() -> None:
    args = ["lark-cli", "auth", "status"]

    assert prepare_command_args(args, os_name="posix") == args


def test_windows_cmd_launcher_uses_comspec_and_preserves_arguments() -> None:
    args = ["lark-cli", "im", "+messages-send", "--file", "日报 文件.md"]

    prepared = prepare_command_args(
        args,
        os_name="nt",
        environ={"ComSpec": r"C:\Windows\System32\cmd.exe"},
        which=lambda command: (
            r"C:\Users\Test User\AppData\Roaming\npm\lark-cli.cmd"
            if command == "lark-cli.cmd"
            else None
        ),
    )

    assert prepared[:4] == [
        r"C:\Windows\System32\cmd.exe",
        "/d",
        "/s",
        "/c",
    ]
    assert prepared[4] == subprocess.list2cmdline(
        [
            r"C:\Users\Test User\AppData\Roaming\npm\lark-cli.cmd",
            *args[1:],
        ]
    )


def test_windows_codex_exe_is_launched_directly() -> None:
    args = ["codex", "--version"]
    launcher = r"C:\Program Files\Codex\codex.exe"

    prepared = prepare_command_args(
        args,
        os_name="nt",
        environ={"ComSpec": r"C:\Windows\System32\cmd.exe"},
        which=lambda command: launcher if command == "codex" else None,
    )

    assert prepared == [launcher, "--version"]


def test_windows_codex_without_launcher_fails_clearly() -> None:
    args = ["codex", "--version"]

    with pytest.raises(FileNotFoundError, match=r"codex\.cmd or codex\.exe"):
        prepare_command_args(
            args,
            os_name="nt",
            environ={},
            which=lambda command: None,
        )


def test_run_text_command_decodes_utf8_and_replaces_invalid_bytes() -> None:
    result = run_text_command(
        (
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write('日报'.encode('utf-8') + b'\\xff')",
        )
    )

    assert result.returncode == 0
    assert result.stdout == "日报\ufffd"
