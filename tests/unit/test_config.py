from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from src.worktrace.config import (
    DEFAULT_COLLECTED_GROUP_REASON_DEFINITIONS,
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


def test_load_online_llm_settings_reads_stream_tls_and_reasoning_overrides(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_LLM_BASE_URL=https://llm.example/v1\n"
        "WORKTRACE_LLM_MODEL=provider-model\n"
        "WORKTRACE_LLM_API_KEY=file-key\n"
        "WORKTRACE_LLM_STREAM=true\n"
        "WORKTRACE_LLM_TLS_VERIFY=true\n"
        "WORKTRACE_LLM_REASONING_EFFORT=none\n",
        encoding="utf-8",
    )

    settings = load_online_llm_settings(RuntimeConfig(), cwd=tmp_path, environ={})

    assert settings.stream_enabled is True
    assert settings.tls_verify is True
    assert settings.reasoning_effort == "none"


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


def test_runtime_config_uses_model_input_batch_target_by_default() -> None:
    config = RuntimeConfig()

    assert config.model_input_batch_target_tokens == 5200
    assert not hasattr(config, "collected_merge_prompt_char_threshold")


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


def test_load_runtime_config_overrides_reads_conversation_window_settings(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "conversation_window.json").write_text(
        json.dumps(
            {
                "max_anchor_gap_minutes": 11,
                "max_unrelated_intervening_messages": 4,
                "initial_context_messages_before": 2,
                "context_expansion_messages_per_direction": 8,
                "context_expansion_round_limit": 2,
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)

    assert config.max_anchor_gap_minutes == 11
    assert config.max_unrelated_intervening_messages == 4
    assert config.initial_context_messages_before == 2
    assert config.context_expansion_messages_per_direction == 8
    assert config.context_expansion_round_limit == 2


def test_load_runtime_config_overrides_reads_llm_retry_settings(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "llm_retry.json").write_text(
        json.dumps(
            {
                "online_request_retry_limit": 2,
                "segmentation_retry_limit": 5,
                "event_extraction_retry_limit": 6,
                "stream_first_response_timeout_seconds": 61,
                "max_concurrent_llm_requests": 3,
                "max_concurrent_event_extraction_requests": 5,
                "max_concurrent_personal_fact_review_requests": 3,
                "codex_request_interval_min_seconds": 0,
                "codex_request_interval_max_seconds": 1,
                "max_concurrent_collected_merge_review_requests": 3,
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)

    assert config.online_request_retry_limit == 2
    assert config.anchor_retry_limit == 5
    assert config.analysis_batch_retry_limit == 6
    assert config.stream_first_response_timeout_seconds == 61
    assert config.max_concurrent_llm_requests == 3
    assert config.max_concurrent_event_extraction_requests == 5
    assert config.max_concurrent_personal_fact_review_requests == 3
    assert config.codex_request_interval_min_seconds == 0
    assert config.codex_request_interval_max_seconds == 1
    assert config.max_concurrent_collected_merge_review_requests == 3


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
        "WORKTRACE_COLLECTED_MERGE_MISSING_FIELD_RETRY_LIMIT=2\n",
        encoding="utf-8",
    )

    config = load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)

    assert config.collected_merge_trace_enabled is True
    assert config.collected_merge_trace_root == Path("custom-trace")
    assert config.collected_merge_missing_field_retry_ratio == 0.35
    assert config.collected_merge_missing_field_retry_limit == 2


def test_load_runtime_config_overrides_reads_collected_merge_review_config(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "collected_merge.json").write_text(
        json.dumps(
            {
                "high_risk_review_enabled": False,
                "high_risk_source_event_count": 12,
                "high_risk_source_file_count": 5,
                "review_cross_batch_groups": False,
                "review_repaired_groups": True,
                "review_workstream_conflicts": False,
                "review_same_conversation_only_groups": True,
                "review_semantic_only_object_conflicts": False,
                "review_broad_object_groups": False,
                "group_reason_definitions": [
                    asdict(item)
                    for item in DEFAULT_COLLECTED_GROUP_REASON_DEFINITIONS
                ],
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)

    assert config.high_risk_review_enabled is False
    assert config.high_risk_source_event_count == 12
    assert config.high_risk_source_file_count == 5
    assert config.review_cross_batch_groups is False
    assert config.review_repaired_groups is True
    assert config.review_workstream_conflicts is False
    assert config.review_same_conversation_only_groups is True
    assert config.review_semantic_only_object_conflicts is False
    assert config.review_broad_object_groups is False
    assert config.collected_group_reason_definitions[-1].key == (
        "same_deliverable_batch"
    )


def test_repo_collected_merge_config_matches_review_defaults() -> None:
    payload = json.loads(
        Path("config/collected_merge.json").read_text(encoding="utf-8")
    )

    assert payload["high_risk_review_enabled"] is True
    assert payload["high_risk_source_event_count"] == 10
    assert payload["high_risk_source_file_count"] == 4
    assert payload["review_cross_batch_groups"] is True
    assert payload["review_repaired_groups"] is True
    assert payload["review_workstream_conflicts"] is True
    assert payload["review_same_conversation_only_groups"] is True
    assert payload["review_semantic_only_object_conflicts"] is True
    assert payload["review_broad_object_groups"] is True
    definitions = {item["key"]: item for item in payload["group_reason_definitions"]}
    assert definitions["same_object"]["acceptance_rules"]
    assert definitions["same_object"]["rejection_rules"]
    assert definitions["continuous_action"]["acceptance_rules"]
    assert definitions["same_deliverable_batch"]["rejection_rules"]


@pytest.mark.parametrize(
    "payload",
    [
        {"high_risk_review_enabled": True},
        {
            "high_risk_review_enabled": True,
            "high_risk_source_event_count": 0,
            "high_risk_source_file_count": 4,
            "review_cross_batch_groups": True,
            "review_repaired_groups": True,
            "review_workstream_conflicts": True,
            "review_same_conversation_only_groups": True,
        },
        {
            "high_risk_review_enabled": "yes",
            "high_risk_source_event_count": 10,
            "high_risk_source_file_count": 4,
            "review_cross_batch_groups": True,
            "review_repaired_groups": True,
            "review_workstream_conflicts": True,
        },
    ],
)
def test_load_runtime_config_overrides_rejects_invalid_collected_merge_config(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "collected_merge.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Invalid collected merge config"):
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
    assert "离职" in payload["sensitive_event_keywords"]
    assert "招聘" in payload["sensitive_event_keywords"]
    assert "offer" in payload["sensitive_event_keywords"]
    assert "挽留谈判" in payload["sensitive_event_keywords"]
    assert "挽留报价" in payload["sensitive_event_keywords"]
    assert "人员留任" in payload["sensitive_event_keywords"]
    assert "git pull" in payload["excluded_event_keywords"]
    assert "merged.md" in payload["excluded_event_keywords"]
    assert "WorkTrace" in payload["excluded_event_keywords"]
    assert "skills.gydev.cn" in payload["excluded_event_keywords"]
    assert "麻烦" in payload["self_assignment_keywords"]
    assert "处理" in payload["self_assignment_keywords"]


def test_repo_retention_policy_is_loaded_from_config() -> None:
    config = load_runtime_config_overrides(RuntimeConfig(), cwd=Path.cwd())
    policy = config.retention_policy

    assert policy.review_enabled is True
    assert policy.review_retention_reasons == ("follow_up_assigned",)
    assert policy.require_empty_workstream is True
    assert policy.require_no_referenced_files is True
    assert policy.uncertain_policy == "drop"
    assert policy.fact_review_enabled is True
    assert policy.fact_review_source_message_count == 8
    assert policy.fact_review_source_participant_count == 3
    assert policy.fact_review_max_batch_candidates == 1
    assert policy.fact_review_unsupported_policy == "drop"
    assert "comparison_or_example" in {
        item.key for item in policy.fact_risk_signals
    }
    assert policy.fact_review_rules
    assert "审核" in policy.generic_object_hints
    assert "工作" in policy.repeated_low_information_suffixes
    assert {item.key for item in policy.routine_signals} == {
        "presence_or_availability",
        "simple_acknowledgement_or_wait",
        "information_relay_only",
        "other_routine_coordination",
    }
    assert "explicit_business_follow_up" in {
        item.key for item in policy.substantive_signals
    }


def test_load_runtime_config_overrides_rejects_invalid_retention_policy(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "retention_policy.json").write_text(
        json.dumps({"review": {"enabled": True}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Invalid retention policy config"):
        load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)


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


def test_load_runtime_config_overrides_rejects_invalid_codex_interval(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "llm_retry.json").write_text(
        json.dumps(
            {
                "online_request_retry_limit": 1,
                "segmentation_retry_limit": 3,
                "event_extraction_retry_limit": 3,
                "stream_first_response_timeout_seconds": 60,
                "max_concurrent_llm_requests": 3,
                "max_concurrent_event_extraction_requests": 5,
                "max_concurrent_personal_fact_review_requests": 3,
                "codex_request_interval_min_seconds": 2,
                "codex_request_interval_max_seconds": 1,
                "max_concurrent_collected_merge_review_requests": 3,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="codex_request_interval_min_seconds"):
        load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)


def test_load_runtime_config_overrides_rejects_invalid_online_retry_limit(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "llm_retry.json").write_text(
        json.dumps(
            {
                "online_request_retry_limit": -1,
                "segmentation_retry_limit": 3,
                "event_extraction_retry_limit": 3,
                "stream_first_response_timeout_seconds": 60,
                "max_concurrent_llm_requests": 3,
                "max_concurrent_event_extraction_requests": 5,
                "max_concurrent_personal_fact_review_requests": 3,
                "codex_request_interval_min_seconds": 0,
                "codex_request_interval_max_seconds": 1,
                "max_concurrent_collected_merge_review_requests": 3,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="online_request_retry_limit"):
        load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)
