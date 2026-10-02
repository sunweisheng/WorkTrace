from __future__ import annotations

import json

from src.worktrace.cli import main
from src.worktrace.preflight import PreflightReport


def test_import_command_does_not_require_business_preflight(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "absent"))

    def forbidden(*args, **kwargs):
        raise AssertionError("business preflight must not run")

    assert main(["import-codex-config"], preflight_func=forbidden) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "failed"
    assert not (tmp_path / ".env").exists()


def test_full_preflight_cli_passes_explicit_probe_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    calls = []

    def preflight(config, *, cwd, full_backend_checks=False):
        calls.append(full_backend_checks)
        return PreflightReport(ok=True, details={"online_probe": "ok"})

    assert main(["--preflight-full"], preflight_func=preflight) == 0
    assert calls == [True]
    result = json.loads(capsys.readouterr().out)
    assert result["details"]["online_probe"] == "ok"


def test_preflight_config_error_keeps_safe_json_contract(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WORKTRACE_LLM_MODE", "invalid-synthetic-secret")
    assert main(["--preflight-full"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "failed"
    assert result["details"]["error_code"] == "configuration"
    assert "synthetic-secret" not in json.dumps(result)
