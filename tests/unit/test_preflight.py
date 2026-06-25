from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.worktrace.config import RuntimeConfig
from src.worktrace.preflight import (
    CODEX_PROBE_TIMEOUT_SECONDS,
    CommandResult,
    classify_codex_failure,
    classify_hook_failure,
    run_preflight_checks,
)


def _success_runner_factory(tmp_path: Path):
    def runner(args, *, cwd=None, timeout=None, input_text=None):
        command = tuple(args)
        if command[:3] == ("lark-cli", "auth", "status"):
            return CommandResult(
                returncode=0,
                stdout=(
                    '{"identity":"user","identities":{"user":{"available":true,'
                    '"openId":"ou_123"}}}'
                ),
                stderr="",
            )
        if command[:2] == ("python3", "-m"):
            assert timeout == CODEX_PROBE_TIMEOUT_SECONDS
            assert input_text is not None
            return CommandResult(returncode=0, stdout='{"probe":"ok"}', stderr="")
        raise AssertionError(f"Unexpected command: {command}")

    return runner


def test_preflight_success(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_LLM_BASE_URL=https://llm.example/v1\n"
        "WORKTRACE_LLM_MODEL=provider-model\n"
        "WORKTRACE_LLM_API_KEY=file-key\n",
        encoding="utf-8",
    )
    config = RuntimeConfig(data_root=tmp_path / "data")
    report = run_preflight_checks(
        config,
        cwd=tmp_path,
        command_runner=_success_runner_factory(tmp_path),
        python_version=(3, 13, 0),
    )

    assert report.ok is True
    assert report.error_summary == ""
    assert report.details["timezone"] == "Asia/Shanghai"
    assert report.details["analyzer_backend"] == "hook"
    assert report.details["hook_llm_config"] == "ok"
    assert report.details["hook_probe"] == "ok"


def test_preflight_fails_when_lark_identity_is_not_user(tmp_path: Path) -> None:
    def runner(args, *, cwd=None, timeout=None, input_text=None):
        return CommandResult(
            returncode=0,
            stdout='{"identity":"bot","identities":{"user":{"available":false}}}',
            stderr="",
        )

    report = run_preflight_checks(
        RuntimeConfig(data_root=tmp_path / "data"),
        cwd=tmp_path,
        command_runner=runner,
        python_version=(3, 13, 0),
    )

    assert report.ok is False
    assert report.error_summary == "lark-cli is not using a user identity."


def test_preflight_fails_on_codex_timeout(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_LLM_BASE_URL=https://llm.example/v1\n"
        "WORKTRACE_LLM_MODEL=provider-model\n"
        "WORKTRACE_LLM_API_KEY=file-key\n",
        encoding="utf-8",
    )
    def runner(args, *, cwd=None, timeout=None, input_text=None):
        command = tuple(args)
        if command[:3] == ("lark-cli", "auth", "status"):
            return CommandResult(
                returncode=0,
                stdout=(
                    '{"identity":"user","identities":{"user":{"available":true,'
                    '"openId":"ou_123"}}}'
                ),
                stderr="",
            )
        raise subprocess.TimeoutExpired(cmd=list(args), timeout=timeout or 1)

    report = run_preflight_checks(
        RuntimeConfig(data_root=tmp_path / "data"),
        cwd=tmp_path,
        command_runner=runner,
        python_version=(3, 13, 0),
    )

    assert report.ok is False
    assert report.error_summary == "Hook probe timed out."


def test_preflight_fails_when_online_llm_config_is_missing(tmp_path: Path) -> None:
    report = run_preflight_checks(
        RuntimeConfig(data_root=tmp_path / "data"),
        cwd=tmp_path,
        command_runner=_success_runner_factory(tmp_path),
        python_version=(3, 13, 0),
    )

    assert report.ok is False
    assert "Missing online LLM configuration" in report.error_summary


def test_preflight_allows_explicit_codex_stdin_hook_without_online_llm(tmp_path: Path) -> None:
    report = run_preflight_checks(
        RuntimeConfig(
            data_root=tmp_path / "data",
            hook_command="python3 -m src.worktrace.hook_runner --mode codex-stdin",
        ),
        cwd=tmp_path,
        command_runner=_success_runner_factory(tmp_path),
        python_version=(3, 13, 0),
    )

    assert report.ok is True


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        ("", "please login first", "Codex is not logged in or lacks permission."),
        ("network unreachable", "", "Codex network or service is unreachable."),
        ("other", "", "Codex probe failed."),
    ],
)
def test_classify_codex_failure(stdout: str, stderr: str, expected: str) -> None:
    assert (
        classify_codex_failure(CommandResult(returncode=1, stdout=stdout, stderr=stderr))
        == expected
    )


def test_classify_hook_failure() -> None:
    result = CommandResult(
        returncode=1,
        stdout="",
        stderr="stream disconnected - retrying sampling request\nERROR: unexpected status 503 Service Unavailable",
    )

    assert (
        classify_hook_failure(result)
        == "Hook analyzer upstream provider or service is temporarily unavailable."
    )


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("HTTP 401: bad key", "Hook analyzer online LLM API key is invalid or lacks permission."),
        ("HTTP 429: rate limit", "Hook analyzer online LLM is rate limited."),
        ("Network error: connection reset", "Hook analyzer network or service is unreachable."),
        (
            "Missing online LLM configuration: WORKTRACE_LLM_API_KEY",
            "Online LLM configuration is missing. Configure local .env or environment variables before running WorkTrace.",
        ),
    ],
)
def test_classify_hook_failure_variants(stderr: str, expected: str) -> None:
    assert classify_hook_failure(CommandResult(returncode=1, stdout="", stderr=stderr)) == expected


def test_run_subprocess_supports_stdin_input(tmp_path: Path) -> None:
    script = tmp_path / "echo_stdin.py"
    script.write_text(
        "import sys\n"
        "data = sys.stdin.read()\n"
        "sys.stdout.write(data.upper())\n",
        encoding="utf-8",
    )

    result = __import__("src.worktrace.preflight", fromlist=["run_subprocess"]).run_subprocess(
        ("python3", str(script)),
        input_text="hello",
    )

    assert result.returncode == 0
    assert result.stdout == "HELLO"
