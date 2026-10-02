from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.worktrace.config import load_local_env_file


def setup_config(tmp_path):
    codex_home = tmp_path / "Codex 认证"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('''
model = "synthetic-model"
model_reasoning_effort = "high"
model_provider = "relay"
[model_providers.relay]
name = "Synthetic"
base_url = "https://example.invalid/v1"
wire_api = "responses"
requires_openai_auth = false
experimental_bearer_token = "synthetic-private-key"
''', encoding="utf-8")
    return codex_home


def test_preview_is_safe_and_does_not_write(tmp_path):
    from src.worktrace.codex_config_import import import_codex_config

    home = setup_config(tmp_path)
    result = import_codex_config(cwd=tmp_path, environ={"CODEX_HOME": str(home)})
    assert result.status == "preview"
    assert result.to_dict()["values"]["WORKTRACE_CODEX_REASONING_EFFORT"] == "high"
    assert result.to_dict()["auth_source"] == "provider_bearer_token"
    assert "synthetic-private-key" not in json.dumps(result.to_dict())
    assert not (tmp_path / ".env").exists()


def test_apply_preserves_existing_values_and_comments(tmp_path):
    from src.worktrace.codex_config_import import import_codex_config

    home = setup_config(tmp_path)
    env = tmp_path / ".env"
    env.write_text("# 保留配置\nWORKTRACE_CODEX_REASONING_EFFORT=high\n"
                   "WORKTRACE_CODEX_MODEL=existing-model\n", encoding="utf-8")
    result = import_codex_config(cwd=tmp_path, apply=True,
                                 environ={"CODEX_HOME": str(home)})
    assert result.status == "applied"
    values = load_local_env_file(env)
    assert values["WORKTRACE_CODEX_MODEL"] == "existing-model"
    assert values["WORKTRACE_CODEX_REASONING_EFFORT"] == "high"
    assert values["WORKTRACE_CODEX_PROVIDER_BASE_URL"] == "https://example.invalid/v1"
    assert "# 保留配置" in env.read_text(encoding="utf-8")
    assert "synthetic-private-key" not in env.read_text(encoding="utf-8")


def test_auth_conversion_uses_stdin_and_an_isolated_environment(tmp_path):
    from src.worktrace.codex_config_import import import_codex_config

    home = setup_config(tmp_path)
    calls = []

    def command(args, **kwargs):
        calls.append((args, kwargs))
        if "status" in args:
            return SimpleNamespace(returncode=1, stdout="", stderr="Not logged in")
        return SimpleNamespace(returncode=0, stdout="synthetic-private-key", stderr="")

    result = import_codex_config(
        cwd=tmp_path, apply=True, import_auth=True, command_runner=command,
        environ={"CODEX_HOME": str(home), "OPENAI_API_KEY": "do-not-inherit"},
    )
    assert result.status == "applied"
    login = calls[-1]
    assert "--with-api-key" in login[0]
    assert login[1]["input_text"] == "synthetic-private-key\n"
    assert "synthetic-private-key" not in repr(login[0])
    assert "OPENAI_API_KEY" not in login[1]["env"]
    assert "synthetic-private-key" not in json.dumps(result.to_dict())
    assert load_local_env_file(tmp_path / ".env")[
        "WORKTRACE_CODEX_PROVIDER_REQUIRES_OPENAI_AUTH"] == "true"


@pytest.mark.parametrize("existing_auth", [True, False])
def test_existing_auth_or_conflicting_provider_blocks_conversion(tmp_path, existing_auth):
    from src.worktrace.codex_config_import import import_codex_config

    home = setup_config(tmp_path)
    if existing_auth:
        (home / "auth.json").write_text("{}", encoding="utf-8")
    else:
        (tmp_path / ".env").write_text(
            "WORKTRACE_CODEX_PROVIDER_ID=other\n", encoding="utf-8")
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1, stdout="", stderr="Not logged in")

    result = import_codex_config(cwd=tmp_path, apply=True, import_auth=True,
        environ={"CODEX_HOME": str(home)}, command_runner=command)
    assert result.status == "blocked"
    assert not any("--with-api-key" in args for args in calls)


def test_profile_selection_and_missing_config(tmp_path):
    from src.worktrace.codex_config_import import import_codex_config

    home = setup_config(tmp_path)
    with (home / "config.toml").open("a", encoding="utf-8") as out:
        out.write('\nprofile = "unused"\n[profiles.office]\n'
                  'model = "profile-model"\nmodel_reasoning_effort = "medium"\n')
    result = import_codex_config(cwd=tmp_path, profile="office",
                                environ={"CODEX_HOME": str(home)})
    assert result.to_dict()["values"]["WORKTRACE_CODEX_MODEL"] == "profile-model"
    result = import_codex_config(cwd=tmp_path,
                                environ={"CODEX_HOME": str(tmp_path / "absent")})
    assert result.status == "failed"


def test_auth_import_requires_apply(tmp_path):
    from src.worktrace.codex_config_import import import_codex_config

    result = import_codex_config(cwd=tmp_path, import_auth=True)
    assert result.status == "blocked"


def test_import_fills_duplicate_empty_keys(tmp_path):
    from src.worktrace.codex_config_import import import_codex_config

    home = setup_config(tmp_path)
    env = tmp_path / ".env"
    env.write_text("WORKTRACE_CODEX_MODEL=\nWORKTRACE_CODEX_MODEL=\n", encoding="utf-8")
    result = import_codex_config(cwd=tmp_path, apply=True,
                                environ={"CODEX_HOME": str(home)})
    assert result.status == "applied"
    assert load_local_env_file(env)["WORKTRACE_CODEX_MODEL"] == "synthetic-model"


def test_preview_does_not_echo_credentials_in_existing_provider_url(tmp_path):
    from src.worktrace.codex_config_import import import_codex_config

    home = setup_config(tmp_path)
    (tmp_path / ".env").write_text(
        "WORKTRACE_CODEX_PROVIDER_BASE_URL=https://user:synthetic-secret@example.invalid/v1\n",
        encoding="utf-8")
    result = import_codex_config(cwd=tmp_path, environ={"CODEX_HOME": str(home)})
    assert result.status == "failed"
    assert "synthetic-secret" not in json.dumps(result.to_dict())


def test_fresh_installer_template_can_import_personal_settings(tmp_path):
    from src.worktrace.codex_config_import import import_codex_config

    home = setup_config(tmp_path)
    template = Path(__file__).resolve().parents[2] / ".env.example"
    (tmp_path / ".env").write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    result = import_codex_config(cwd=tmp_path, apply=True,
                                environ={"CODEX_HOME": str(home)})
    assert result.status == "applied"
    values = load_local_env_file(tmp_path / ".env")
    assert values["WORKTRACE_CODEX_MODEL"] == "synthetic-model"
    assert values["WORKTRACE_CODEX_REASONING_EFFORT"] == "high"
    assert values["WORKTRACE_CODEX_PROVIDER_REQUIRES_OPENAI_AUTH"] == "false"


def test_existing_malformed_url_returns_json_result(tmp_path):
    from src.worktrace.codex_config_import import import_codex_config

    home = setup_config(tmp_path)
    (tmp_path / ".env").write_text(
        "WORKTRACE_CODEX_PROVIDER_BASE_URL=https://[invalid\n", encoding="utf-8")
    result = import_codex_config(cwd=tmp_path, environ={"CODEX_HOME": str(home)})
    assert result.to_dict()["status"] == "failed"
