from __future__ import annotations

from pathlib import Path

import pytest

from src.worktrace.config import (
    RuntimeConfig,
    load_hook_llm_settings,
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


def test_load_hook_llm_settings_reads_local_env(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_LLM_BASE_URL=https://llm.example/v1\n"
        "WORKTRACE_LLM_MODEL=provider-model\n"
        "WORKTRACE_LLM_API_KEY=file-key\n"
        "WORKTRACE_LLM_TIMEOUT_SECONDS=45\n",
        encoding="utf-8",
    )

    settings = load_hook_llm_settings(RuntimeConfig(), cwd=tmp_path, environ={})

    assert settings.base_url == "https://llm.example/v1"
    assert settings.model == "provider-model"
    assert settings.api_key == "file-key"
    assert settings.timeout_seconds == 45


def test_load_hook_llm_settings_prefers_process_environment(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_LLM_BASE_URL=https://llm.example/v1\n"
        "WORKTRACE_LLM_MODEL=file-model\n"
        "WORKTRACE_LLM_API_KEY=file-key\n",
        encoding="utf-8",
    )

    settings = load_hook_llm_settings(
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


def test_load_hook_llm_settings_requires_all_required_values(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_LLM_BASE_URL=https://llm.example/v1\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_hook_llm_settings(RuntimeConfig(), cwd=tmp_path, environ={})

    assert "Missing online LLM configuration" in str(exc_info.value)
    assert "Do not commit real secrets to git" in str(exc_info.value)


def test_load_hook_llm_settings_requires_positive_integer_timeout(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_LLM_BASE_URL=https://llm.example/v1\n"
        "WORKTRACE_LLM_MODEL=provider-model\n"
        "WORKTRACE_LLM_API_KEY=file-key\n"
        "WORKTRACE_LLM_TIMEOUT_SECONDS=zero\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_hook_llm_settings(RuntimeConfig(), cwd=tmp_path, environ={})

    assert "must be an integer" in str(exc_info.value)
