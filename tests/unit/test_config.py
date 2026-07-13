from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.worktrace.config import (
    RuntimeConfig,
    load_conversation_blacklist_overrides,
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


def test_load_online_llm_settings_reads_false_stream_override(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_LLM_BASE_URL=https://llm.example/v1\n"
        "WORKTRACE_LLM_MODEL=provider-model\n"
        "WORKTRACE_LLM_API_KEY=file-key\n"
        "WORKTRACE_LLM_STREAM=false\n",
        encoding="utf-8",
    )

    settings = load_online_llm_settings(
        RuntimeConfig(llm_stream_enabled=True),
        cwd=tmp_path,
        environ={},
    )

    assert settings.stream_enabled is False


def test_runtime_config_disables_streaming_by_default() -> None:
    assert RuntimeConfig().llm_stream_enabled is False


def test_load_runtime_config_overrides_reads_rule_lists(
    tmp_path: Path,
) -> None:
    rules_dir = tmp_path / "config"
    rules_dir.mkdir()
    (rules_dir / "event_rules.json").write_text(
        (
            "{\n"
            '  "sensitive_event_keywords": ["工资", "薪资", "吵架"],\n'
            '  "excluded_event_keywords": ["代码同步", "git pull"],\n'
            '  "self_assignment_keywords": ["麻烦", "请", "处理", "确认"]\n'
            "}\n"
        ),
        encoding="utf-8",
    )

    config = load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)

    assert config.sensitive_event_keywords == ("工资", "薪资", "吵架")
    assert config.excluded_event_keywords == (
        "代码同步",
        "git pull",
    )
    assert config.self_assignment_keywords == ("麻烦", "请", "处理", "确认")


def test_load_runtime_config_overrides_uses_defaults_when_rule_file_missing(
    tmp_path: Path,
) -> None:
    config = load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)

    assert config.sensitive_event_keywords == ()
    assert config.excluded_event_keywords == ()
    assert config.self_assignment_keywords == ()


def test_load_runtime_config_overrides_reads_self_relation_metadata(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "event_metadata.json").write_text(
        json.dumps(
            {
                "self_relations": [
                    {"key": "collaboration", "label": "协作参与", "order": 20},
                    {"key": "initiated", "label": "发起", "order": 10},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    config = load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)

    assert [item.key for item in config.self_relation_types] == [
        "initiated",
        "collaboration",
    ]
    assert [item.label for item in config.self_relation_types] == ["发起", "协作参与"]


def test_repo_event_metadata_defines_self_relation_labels_and_order() -> None:
    payload = json.loads(Path("config/event_metadata.json").read_text(encoding="utf-8"))

    assert [item["key"] for item in payload["self_relations"]] == [
        "initiated",
        "primary_execution",
        "collaboration",
        "decision_confirmation",
        "feedback_acceptance",
        "assigned",
        "response_only",
    ]


def test_load_runtime_config_overrides_reads_collected_merge_env_overrides(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_COLLECTED_MERGE_TRACE=true\n"
        "WORKTRACE_COLLECTED_MERGE_TRACE_ROOT=custom-trace\n"
        "WORKTRACE_COLLECTED_MERGE_MISSING_FIELD_RETRY_RATIO=0.35\n"
        "WORKTRACE_COLLECTED_MERGE_MISSING_FIELD_RETRY_LIMIT=2\n"
        "WORKTRACE_COLLECTED_MERGE_RETRYABLE_ERROR_LIMIT=3\n"
        "WORKTRACE_COLLECTED_MERGE_RETRY_DELAY_SECONDS=4.5\n",
        encoding="utf-8",
    )

    config = load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)

    assert config.collected_merge_trace_enabled is True
    assert config.collected_merge_trace_root == Path("custom-trace")
    assert config.collected_merge_missing_field_retry_ratio == 0.35
    assert config.collected_merge_missing_field_retry_limit == 2
    assert config.collected_merge_retryable_error_limit == 3
    assert config.collected_merge_retry_delay_seconds == 4.5


@pytest.mark.parametrize(
    ("env_name", "env_value"),
    [
        ("WORKTRACE_COLLECTED_MERGE_RETRYABLE_ERROR_LIMIT", "-1"),
        ("WORKTRACE_COLLECTED_MERGE_RETRY_DELAY_SECONDS", "-0.1"),
    ],
)
def test_load_runtime_config_overrides_rejects_invalid_collected_merge_retry(
    tmp_path: Path,
    env_name: str,
    env_value: str,
) -> None:
    (tmp_path / ".env").write_text(
        f"{env_name}={env_value}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)


def test_load_runtime_config_overrides_rejects_invalid_rule_file(tmp_path: Path) -> None:
    rules_dir = tmp_path / "config"
    rules_dir.mkdir()
    (rules_dir / "event_rules.json").write_text('{"sensitive_event_keywords":"bad"}', encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)

    assert "event rules config" in str(exc_info.value)


def test_repo_event_rules_use_rule_lists_only() -> None:
    payload = json.loads(Path("config/event_rules.json").read_text(encoding="utf-8"))

    assert "劳动仲裁" in payload["sensitive_event_keywords"]
    assert "绩效" in payload["sensitive_event_keywords"]
    assert "git pull" in payload["excluded_event_keywords"]
    assert "麻烦" in payload["self_assignment_keywords"]
    assert "处理" in payload["self_assignment_keywords"]


def test_load_runtime_config_overrides_rejects_legacy_rule_keys(tmp_path: Path) -> None:
    rules_dir = tmp_path / "config"
    rules_dir.mkdir()
    (rules_dir / "event_rules.json").write_text(
        '{"confidential_event_keywords":["薪资"]}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)

    assert "legacy keys" in str(exc_info.value)


def test_load_conversation_blacklist_overrides_reads_ids_and_dedupes(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "conversation_blacklist.json").write_text(
        (
            "{\n"
            '  "excluded_conversation_ids": [" oc_1 ", "", "oc_2", "oc_1"]\n'
            "}\n"
        ),
        encoding="utf-8",
    )

    config = load_conversation_blacklist_overrides(RuntimeConfig(), cwd=tmp_path)

    assert config.excluded_conversation_ids == ("oc_1", "oc_2")


def test_load_conversation_blacklist_overrides_uses_defaults_when_file_missing(
    tmp_path: Path,
) -> None:
    config = load_conversation_blacklist_overrides(RuntimeConfig(), cwd=tmp_path)

    assert config.excluded_conversation_ids == ()


def test_load_conversation_blacklist_overrides_rejects_invalid_json(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "conversation_blacklist.json").write_text("{bad", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        load_conversation_blacklist_overrides(RuntimeConfig(), cwd=tmp_path)

    assert "conversation blacklist config" in str(exc_info.value)


def test_load_conversation_blacklist_overrides_rejects_invalid_list_shape(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "conversation_blacklist.json").write_text(
        '{"excluded_conversation_ids":["oc_1", 2]}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_conversation_blacklist_overrides(RuntimeConfig(), cwd=tmp_path)

    assert "conversation blacklist config" in str(exc_info.value)


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
