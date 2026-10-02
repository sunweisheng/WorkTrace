from __future__ import annotations

import subprocess
import sys
import os

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

    inner = subprocess.list2cmdline([
        r"C:\Users\Test User\AppData\Roaming\npm\lark-cli.cmd",
        *args[1:],
    ])
    assert prepared == (
        r'C:\Windows\System32\cmd.exe /d /s /v:off /c "' + inner + '"'
    )
    assert '\\"' not in prepared


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


@pytest.mark.skipif(os.name != "nt", reason="Windows native launcher")
@pytest.mark.parametrize("suffix", [".cmd", ".bat"])
@pytest.mark.parametrize("argument", [
    "日报 文件.md", "张&李.md", "file%WT_SYNTHETIC%.md",
    "file!WT_SYNTHETIC!.md", 'name="x&y"', "caret^file.md",
])
def test_windows_real_script_launcher_preserves_chinese_arguments(
    tmp_path, monkeypatch, suffix, argument,
):
    directory = tmp_path / "中文 CLI"
    directory.mkdir()
    payload_script = directory / "argv.py"
    payload_script.write_text(
        "import json,sys; sys.stdout.buffer.write("
        "json.dumps(sys.argv[1:],ensure_ascii=False).encode('utf-8'))",
        encoding="utf-8",
    )
    launcher = directory / ("codex" + suffix)
    launcher.write_text(
        f'@echo off\n"{sys.executable}" "%~dp0argv.py" %*\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("WT_SYNTHETIC", "must-not-expand")
    import json

    result = run_text_command(["codex", argument])
    assert result.returncode == 0
    assert json.loads(result.stdout) == [argument]


def test_windows_cmd_metacharacters_are_protected_without_double_escaping():
    prepared = prepare_command_args(
        ["lark-cli", "im", "+messages-send", "--file", "张&李.md"],
        os_name="nt", environ={"COMSPEC": "cmd.exe"},
        which=lambda name: r"C:\CLI\lark-cli.cmd" if name == "lark-cli" else None,
    )
    assert '张^^^&李.md' in prepared
    assert "/v:off" in prepared


@pytest.mark.parametrize("directory", [
    "R&D", "percent%WT_SYNTHETIC%", "bang!WT_SYNTHETIC!", "caret^CLI",
])
def test_batch_launcher_path_has_only_one_cmd_escape_layer(directory):
    import mslex

    launcher = rf"C:\{directory}\codex.cmd"
    prepared = prepare_command_args(
        ["codex", "张&李.md"], os_name="nt", environ={"COMSPEC": "cmd.exe"},
        which=lambda name: launcher if name == "codex" else None,
    )
    inner = prepared.partition(" /c ")[2][1:-1]
    first_parse = mslex.strip_carets_like_cmd(inner)
    assert mslex.split(first_parse, like_cmd=False)[0] == launcher


@pytest.mark.skipif(os.name != "nt", reason="Windows native launcher")
@pytest.mark.parametrize("suffix", [".cmd", ".bat"])
@pytest.mark.parametrize("directory_name", [
    "R&D", "percent%WT_SYNTHETIC%", "bang!WT_SYNTHETIC!", "caret^CLI",
])
def test_windows_real_launcher_path_preserves_special_characters(
    tmp_path, monkeypatch, suffix, directory_name,
):
    import json

    directory = tmp_path / directory_name
    directory.mkdir()
    (directory / "argv.py").write_text(
        "import json,sys; sys.stdout.buffer.write("
        "json.dumps(sys.argv[1:],ensure_ascii=False).encode('utf-8'))",
        encoding="utf-8",
    )
    (directory / ("codex" + suffix)).write_text(
        f'@echo off\n"{sys.executable}" "%~dp0argv.py" %*\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("WT_SYNTHETIC", "must-not-expand")

    result = run_text_command(["codex", "张&李.md"])
    assert result.returncode == 0
    assert json.loads(result.stdout) == ["张&李.md"]


@pytest.mark.parametrize("argument", [
    "张&李.md", "file%WT_SYNTHETIC%.md", "file!WT_SYNTHETIC!.md",
    'name="x&y"', "caret^file.md", "带 空格^文件.md", "C:\\目录\\",
])
def test_batch_forwarding_keeps_the_next_cmd_escape_layer(argument):
    import mslex

    prepared = prepare_command_args(
        ["codex", argument], os_name="nt", environ={"COMSPEC": "cmd.exe"},
        which=lambda name: r"C:\中文 CLI\codex.cmd" if name == "codex" else None,
    )
    inner = prepared.partition(" /c ")[2][1:-1]
    forwarded = mslex.strip_carets_like_cmd(inner)
    assert mslex.split(forwarded, like_cmd=True) == [r"C:\中文 CLI\codex.cmd", argument]
