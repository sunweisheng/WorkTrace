from __future__ import annotations

import json

import pytest

from src.worktrace.config import RuntimeConfig
from src.worktrace.preflight import CommandResult, classify_codex_failure


@pytest.mark.parametrize("message, expected", [
    ("HTTP 403 Permission denied secret-key", "Permission denied"),
    ("TLS certificate verify failed secret-key", "TLS"),
    ("Unsupported model_reasoning_effort secret-key", "unsupported"),
    ("HTTP 401 Unauthorized secret-key", "configured provider"),
])
def test_preflight_error_is_actionable_and_does_not_echo_secrets(message, expected):
    summary = classify_codex_failure(CommandResult(1, "", message))
    assert expected in summary
    assert "secret-key" not in summary


def test_lark_reports_uninitialized_config_without_raw_error():
    from src.worktrace.preflight import check_lark_identity
    from src.worktrace.errors import PreflightError

    with pytest.raises(PreflightError, match="not configured") as caught:
        check_lark_identity(lambda args: CommandResult(
            1, json.dumps({"error": {"code": "not_initialized"}}),
            "not initialized synthetic-secret"))
    assert "synthetic-secret" not in str(caught.value)


def test_online_probe_classifies_nested_certificate_failure():
    import ssl
    import httpx
    from openai import APIConnectionError
    from src.worktrace.preflight import classify_online_failure

    error = APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))
    error.__cause__ = ssl.SSLCertVerificationError(
        "certificate verify failed synthetic-secret")
    assert "TLS certificate verification failed" in classify_online_failure(error)
    assert "synthetic-secret" not in classify_online_failure(error)


def test_full_preflight_keeps_original_model_error_category(tmp_path, monkeypatch):
    from src.worktrace.preflight import run_preflight_checks

    monkeypatch.setattr("src.worktrace.preflight.require_command", lambda name: name)
    monkeypatch.setattr("src.worktrace.preflight.check_lark_identity", lambda runner: None)
    monkeypatch.setattr("src.worktrace.preflight.load_codex_llm_settings", lambda *a, **k:
        __import__('types').SimpleNamespace(model="synthetic", reasoning_effort="high"))

    def fail(*args, **kwargs):
        raise RuntimeError("Unsupported model_reasoning_effort synthetic-secret")

    monkeypatch.setattr("src.worktrace.analyzers.codex.CodexAnalyzer.request_function", fail)
    report = run_preflight_checks(RuntimeConfig(data_root=tmp_path / "data"),
                                cwd=tmp_path, full_backend_checks=True)
    assert report.details["codex_probe_error_code"] == "configuration"


def test_full_preflight_probes_fallback_even_if_primary_fails(tmp_path, monkeypatch):
    from src.worktrace.preflight import run_preflight_checks
    from src.worktrace.errors import PreflightError

    (tmp_path / ".env").write_text('''
WORKTRACE_CODEX_MODEL=synthetic-model
WORKTRACE_CODEX_REASONING_EFFORT=high
WORKTRACE_CODEX_PROVIDER_ID=relay
WORKTRACE_CODEX_PROVIDER_NAME=Synthetic
WORKTRACE_CODEX_PROVIDER_BASE_URL=https://example.invalid/v1
WORKTRACE_CODEX_PROVIDER_WIRE_API=responses
WORKTRACE_CODEX_PROVIDER_REQUIRES_OPENAI_AUTH=true
WORKTRACE_LLM_BASE_URL=https://example.invalid/v1
WORKTRACE_LLM_MODEL=synthetic-online
WORKTRACE_LLM_API_KEY=synthetic-secret
WORKTRACE_LLM_REASONING_EFFORT=none
''', encoding="utf-8")
    monkeypatch.setattr("src.worktrace.preflight.require_command", lambda name: name)
    monkeypatch.setattr("src.worktrace.preflight.check_lark_identity", lambda runner: None)

    def fail(*args, **kwargs):
        raise PreflightError("Authentication unavailable.")

    probes = []
    monkeypatch.setattr("src.worktrace.preflight.probe_codex", fail)
    monkeypatch.setattr("src.worktrace.preflight.probe_online_llm", lambda *a, **k:
        probes.append("online") or {"online_probe": "ok", "tls_verify": "false"})
    report = run_preflight_checks(RuntimeConfig(data_root=tmp_path / "data"),
        cwd=tmp_path, full_backend_checks=True)
    assert not report.ok
    assert probes == ["online"]
    assert report.details["codex_probe"] == "failed"
    assert report.details["online_probe"] == "ok"
    assert report.details["tls_verify"] == "false"
    assert "synthetic-secret" not in repr(report)
