from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from src.worktrace.config import RuntimeConfig
from src.worktrace.preflight import (
    CommandResult,
    classify_codex_failure,
    classify_online_failure,
    run_preflight_checks,
)


def _success_runner_factory():
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
        raise AssertionError(f"Unexpected command: {command}")

    return runner


def test_preflight_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_LLM_BASE_URL=https://llm.example/v1\n"
        "WORKTRACE_LLM_MODEL=provider-model\n"
        "WORKTRACE_LLM_API_KEY=file-key\n",
        encoding="utf-8",
    )

    class FakeResponse:
        def model_dump(self):
            return {"output_text": '{"probe":"ok"}'}

    class FakeResponses:
        def create(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        def __init__(self, **kwargs):
            self.responses = FakeResponses()

    class FakeHttpClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("src.worktrace.preflight.httpx.Client", lambda **kwargs: FakeHttpClient())
    monkeypatch.setattr("src.worktrace.preflight.OpenAI", lambda **kwargs: FakeClient())

    report = run_preflight_checks(
        RuntimeConfig(data_root=tmp_path / "data"),
        cwd=tmp_path,
        command_runner=_success_runner_factory(),
        python_version=(3, 13, 0),
    )

    assert report.ok is True
    assert report.details["analyzer_backend"] == "online"
    assert report.details["online_llm_config"] == "ok"
    assert report.details["online_probe"] == "ok"
    assert report.details["certificate_verification"] == "disabled"


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


def test_preflight_fails_on_online_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_LLM_BASE_URL=https://llm.example/v1\n"
        "WORKTRACE_LLM_MODEL=provider-model\n"
        "WORKTRACE_LLM_API_KEY=file-key\n",
        encoding="utf-8",
    )

    class FakeHttpClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeClient:
        def __init__(self, **kwargs):
            class FakeResponses:
                def create(self, **kwargs):
                    from openai import APITimeoutError

                    raise APITimeoutError(request=httpx.Request("POST", "https://llm.example/v1/responses"))

            self.responses = FakeResponses()

    monkeypatch.setattr("src.worktrace.preflight.httpx.Client", lambda **kwargs: FakeHttpClient())
    monkeypatch.setattr("src.worktrace.preflight.OpenAI", lambda **kwargs: FakeClient())

    report = run_preflight_checks(
        RuntimeConfig(data_root=tmp_path / "data"),
        cwd=tmp_path,
        command_runner=_success_runner_factory(),
        python_version=(3, 13, 0),
    )

    assert report.ok is False
    assert report.error_summary == "Online LLM probe timed out."


def test_preflight_fails_when_online_llm_config_is_missing(tmp_path: Path) -> None:
    report = run_preflight_checks(
        RuntimeConfig(data_root=tmp_path / "data"),
        cwd=tmp_path,
        command_runner=_success_runner_factory(),
        python_version=(3, 13, 0),
    )

    assert report.ok is False
    assert "Missing online LLM configuration" in report.error_summary


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        ("", "please login first", "Codex is not logged in or lacks permission."),
        ("network unreachable", "", "Codex network or service is unreachable."),
        ("other", "", "Codex probe failed."),
    ],
)
def test_classify_codex_failure(stdout: str, stderr: str, expected: str) -> None:
    assert classify_codex_failure(CommandResult(returncode=1, stdout=stdout, stderr=stderr)) == expected


def test_classify_online_failure_http_429() -> None:
    from openai import RateLimitError

    request = httpx.Request("POST", "https://llm.example/v1/responses")
    response = httpx.Response(429, request=request)
    error = RateLimitError("rate limited", response=response, body=None)

    assert classify_online_failure(error) == "Online LLM is rate limited."


def test_classify_online_failure_tls() -> None:
    from openai import APIConnectionError

    error = APIConnectionError(
        message="certificate verify failed",
        request=httpx.Request("POST", "https://llm.example/v1/responses"),
    )

    assert classify_online_failure(error) == "Online LLM TLS certificate verification failed."


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
