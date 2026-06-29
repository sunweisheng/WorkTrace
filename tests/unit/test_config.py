from __future__ import annotations

from pathlib import Path

import pytest

from src.worktrace.config import (
    RuntimeConfig,
    load_runtime_config_overrides,
    load_online_llm_settings,
    parse_dotenv_lines,
)


def test_parse_dotenv_lines_supports_comments_quotes_and_export() -> None:
    values = parse_dotenv_lines(
        """
        # comment
        export WORKTRACE_LLM_BASE_URL="https://example.com/v1"
        WORKTRACE_LLM_MODEL='gpt-compatible'
        WORKTRACE_LLM_API_KEY=secret-key
        INVALID_LINE
        """
    )

    assert values == {
        "WORKTRACE_LLM_BASE_URL": "https://example.com/v1",
        "WORKTRACE_LLM_MODEL": "gpt-compatible",
        "WORKTRACE_LLM_API_KEY": "secret-key",
    }


def test_load_online_llm_settings_reads_local_env(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_LLM_BASE_URL=https://llm.example/v1\n"
        "WORKTRACE_LLM_MODEL=provider-model\n"
        "WORKTRACE_LLM_API_KEY=file-key\n"
        "WORKTRACE_LLM_TIMEOUT_SECONDS=45\n",
        encoding="utf-8",
    )

    settings = load_online_llm_settings(RuntimeConfig(), cwd=tmp_path, environ={})

    assert settings.base_url == "https://llm.example/v1"
    assert settings.model == "provider-model"
    assert settings.api_key == "file-key"
    assert settings.timeout_seconds == 45
    assert settings.stream_enabled is False
    assert settings.tls_verify is False
    assert settings.reasoning_effort == "none"


def test_load_online_llm_settings_prefers_process_environment(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_LLM_BASE_URL=https://llm.example/v1\n"
        "WORKTRACE_LLM_MODEL=file-model\n"
        "WORKTRACE_LLM_API_KEY=file-key\n",
        encoding="utf-8",
    )

    settings = load_online_llm_settings(
        RuntimeConfig(),
        cwd=tmp_path,
        environ={
            "WORKTRACE_LLM_MODEL": "env-model",
            "WORKTRACE_LLM_API_KEY": "env-key",
        },
    )

    assert settings.base_url == "https://llm.example/v1"
    assert settings.model == "env-model"
    assert settings.api_key == "env-key"


def test_load_online_llm_settings_requires_all_required_values(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_LLM_BASE_URL=https://llm.example/v1\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_online_llm_settings(RuntimeConfig(), cwd=tmp_path, environ={})

    assert "Missing online LLM configuration" in str(exc_info.value)
    assert "requires the user to provide" in str(exc_info.value)
    assert "Do not commit real secrets to git" in str(exc_info.value)


def test_load_online_llm_settings_requires_positive_integer_timeout(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_LLM_BASE_URL=https://llm.example/v1\n"
        "WORKTRACE_LLM_MODEL=provider-model\n"
        "WORKTRACE_LLM_API_KEY=file-key\n"
        "WORKTRACE_LLM_TIMEOUT_SECONDS=zero\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_online_llm_settings(RuntimeConfig(), cwd=tmp_path, environ={})

    assert "must be an integer" in str(exc_info.value)


def test_load_online_llm_settings_reads_stream_tls_and_sleep_overrides(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_LLM_BASE_URL=https://llm.example/v1\n"
        "WORKTRACE_LLM_MODEL=provider-model\n"
        "WORKTRACE_LLM_API_KEY=file-key\n"
        "WORKTRACE_LLM_STREAM=true\n"
        "WORKTRACE_LLM_TLS_VERIFY=true\n"
        "WORKTRACE_LLM_REASONING_EFFORT=none\n"
        "WORKTRACE_LLM_SLEEP_MIN_SECONDS=1.5\n"
        "WORKTRACE_LLM_SLEEP_MAX_SECONDS=2.5\n",
        encoding="utf-8",
    )

    settings = load_online_llm_settings(RuntimeConfig(), cwd=tmp_path, environ={})

    assert settings.stream_enabled is True
    assert settings.tls_verify is True
    assert settings.reasoning_effort == "none"
    assert settings.sleep_min_seconds == 1.5
    assert settings.sleep_max_seconds == 2.5


def test_load_runtime_config_overrides_reads_excluded_event_rules_from_local_env(
    tmp_path: Path,
) -> None:
    rules_dir = tmp_path / "config"
    rules_dir.mkdir()
    (rules_dir / "event_rules.json").write_text(
        (
            "{\n"
            '  "excluded_event_topics": ["代码同步", "工作面谈安排", "故障数据同步"],\n'
            '  "excluded_event_content_signatures": ["git pull", "聆听大老板电话"]\n'
            "}\n"
        ),
        encoding="utf-8",
    )

    config = load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)

    assert config.excluded_event_topics == (
        "代码同步",
        "工作面谈安排",
        "故障数据同步",
    )
    assert config.excluded_event_content_signatures == (
        "git pull",
        "聆听大老板电话",
    )


def test_load_runtime_config_overrides_uses_defaults_when_rule_file_missing(
    tmp_path: Path,
) -> None:
    config = load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)

    assert config.excluded_event_topics == RuntimeConfig().excluded_event_topics


def test_load_runtime_config_overrides_rejects_invalid_rule_file(tmp_path: Path) -> None:
    rules_dir = tmp_path / "config"
    rules_dir.mkdir()
    (rules_dir / "event_rules.json").write_text('{"excluded_event_topics":"bad"}', encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)

    assert "event rules config" in str(exc_info.value)


def test_load_online_llm_settings_rejects_invalid_sleep_range(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_LLM_BASE_URL=https://llm.example/v1\n"
        "WORKTRACE_LLM_MODEL=provider-model\n"
        "WORKTRACE_LLM_API_KEY=file-key\n"
        "WORKTRACE_LLM_SLEEP_MIN_SECONDS=2\n"
        "WORKTRACE_LLM_SLEEP_MAX_SECONDS=1\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_online_llm_settings(RuntimeConfig(), cwd=tmp_path, environ={})

    assert "delay range" in str(exc_info.value)
