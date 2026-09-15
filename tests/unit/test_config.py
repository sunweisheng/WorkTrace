from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.worktrace.config import (
    RuntimeConfig,
    event_generation_debug_summary,
    load_conversation_blacklist_overrides,
    load_codex_llm_settings,
    load_llm_timeout_seconds,
    load_model_input_budget_config,
    load_runtime_config_overrides,
    load_online_llm_settings,
    parse_dotenv_lines,
)


def _write_minimal_runtime_files(root: Path) -> None:
    source_config = Path.cwd() / "config"
    target_config = root / "config"
    target_config.mkdir(parents=True, exist_ok=True)
    for name in (
        "event_rules.json",
        "retention_policy.json",
        "event_metadata.json",
        "conversation_window.json",
        "llm_retry.json",
        "event_grouping.json",
        "event_generation.json",
        "collected_merge.json",
        "self_delivery.json",
    ):
        (target_config / name).write_text(
            (source_config / name).read_text(encoding="utf-8"),
            encoding="utf-8",
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


def test_load_llm_timeout_seconds_does_not_require_online_credentials(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_LLM_TIMEOUT_SECONDS=1200\n",
        encoding="utf-8",
    )

    assert load_llm_timeout_seconds(
        RuntimeConfig(),
        cwd=tmp_path,
        environ={},
    ) == 1200


def test_load_codex_llm_settings_requires_local_values_and_ignores_process_model_values(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_CODEX_MODEL=local-codex-model\n"
        "WORKTRACE_CODEX_REASONING_EFFORT=high\n"
        "WORKTRACE_CODEX_PROVIDER_ID=local-relay\n"
        "WORKTRACE_CODEX_PROVIDER_NAME=Local Relay\n"
        "WORKTRACE_CODEX_PROVIDER_BASE_URL=https://relay.example/v1\n"
        "WORKTRACE_CODEX_PROVIDER_WIRE_API=responses\n"
        "WORKTRACE_CODEX_PROVIDER_REQUIRES_OPENAI_AUTH=true\n",
        encoding="utf-8",
    )

    settings = load_codex_llm_settings(
        RuntimeConfig(),
        cwd=tmp_path,
        environ={
            "WORKTRACE_CODEX_MODEL": "personal-default-model",
            "WORKTRACE_CODEX_REASONING_EFFORT": "low",
        },
    )

    assert settings.model == "local-codex-model"
    assert settings.reasoning_effort == "high"
    assert settings.provider_id == "local-relay"
    assert settings.provider_name == "Local Relay"
    assert settings.provider_base_url == "https://relay.example/v1"
    assert settings.provider_wire_api == "responses"
    assert settings.provider_requires_openai_auth is True


def test_load_codex_llm_settings_rejects_process_only_model_values(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="repository-local"):
        load_codex_llm_settings(
            RuntimeConfig(),
            cwd=tmp_path,
            environ={
                "WORKTRACE_CODEX_MODEL": "personal-default-model",
                "WORKTRACE_CODEX_REASONING_EFFORT": "low",
            },
        )


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

    assert config.model_input_batch_target_tokens == 7000
    assert not hasattr(config, "collected_merge_prompt_char_threshold")


def test_model_input_budget_matches_current_model_pair(tmp_path: Path) -> None:
    _write_minimal_runtime_files(tmp_path)
    (tmp_path / ".env").write_text(
        "WORKTRACE_CODEX_MODEL=primary-model\n"
        "WORKTRACE_LLM_MODEL=fallback-model\n",
        encoding="utf-8",
    )
    (tmp_path / "config" / "model_input_budget.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "default_target_tokens": 7000,
                "profiles": [
                    {
                        "profile_id": "measured-v1",
                        "primary_model": "primary-model",
                        "fallback_model": "fallback-model",
                        "target_tokens": 20000,
                        "benchmark_dataset_version": "dataset-v1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)

    assert config.model_input_batch_target_tokens == 20000
    assert config.model_input_budget_selection.profile_matched is True
    assert config.model_input_budget_selection.profile_id == "measured-v1"
    assert config.model_input_budget_selection.benchmark_dataset_version == "dataset-v1"


def test_model_input_budget_uses_default_without_matching_pair(tmp_path: Path) -> None:
    _write_minimal_runtime_files(tmp_path)
    (tmp_path / ".env").write_text(
        "WORKTRACE_CODEX_MODEL=another-primary\n"
        "WORKTRACE_LLM_MODEL=another-fallback\n",
        encoding="utf-8",
    )
    (tmp_path / "config" / "model_input_budget.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "default_target_tokens": 7000,
                "profiles": [
                    {
                        "profile_id": "measured-v1",
                        "primary_model": "primary-model",
                        "fallback_model": "fallback-model",
                        "target_tokens": 20000,
                        "benchmark_dataset_version": "dataset-v1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)

    assert config.model_input_batch_target_tokens == 7000
    assert config.model_input_budget_selection.profile_matched is False
    assert config.model_input_budget_selection.profile_id == ""


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda payload: payload.update(extra=True), "fields"),
        (lambda payload: payload.pop("profiles"), "fields"),
        (lambda payload: payload.update(schema_version=2), "schema_version"),
        (lambda payload: payload.update(default_target_tokens=0), "positive integer"),
        (lambda payload: payload.update(profiles="invalid"), "non-empty list"),
        (
            lambda payload: payload["profiles"][0].update(target_tokens=True),
            "positive integer",
        ),
        (
            lambda payload: payload["profiles"][0].update(profile_id=""),
            "non-empty",
        ),
        (
            lambda payload: payload["profiles"].append(
                {
                    **payload["profiles"][0],
                    "profile_id": "another-profile",
                }
            ),
            "model combinations",
        ),
    ],
)
def test_model_input_budget_rejects_invalid_contract(
    tmp_path: Path,
    change,
    message: str,
) -> None:
    payload = {
        "schema_version": 1,
        "default_target_tokens": 7000,
        "profiles": [
            {
                "profile_id": "measured-v1",
                "primary_model": "primary-model",
                "fallback_model": "fallback-model",
                "target_tokens": 20000,
                "benchmark_dataset_version": "dataset-v1",
            }
        ],
    }
    change(payload)
    config_path = tmp_path / "model_input_budget.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_model_input_budget_config(config_path)


def test_missing_model_input_budget_file_keeps_7000_default(tmp_path: Path) -> None:
    budget = load_model_input_budget_config(tmp_path / "missing.json")

    assert budget.default_target_tokens == 7000
    assert budget.profiles == ()


def test_repo_event_generation_config_is_loaded_and_anonymized() -> None:
    config = load_runtime_config_overrides(RuntimeConfig(), cwd=Path.cwd())
    generation = config.event_generation
    raw_text = Path("config/event_generation.json").read_text(encoding="utf-8")

    assert generation.schema_version == 1
    assert generation.shared_writing_rules
    assert any("先判断当前输入中的事实" in rule for rule in generation.shared_writing_rules)
    assert any("异常、范围变化、关键决定" in rule for rule in generation.shared_writing_rules)
    assert generation.personal_event_boundary_rules
    assert dict(generation.personal_template).keys() == {
        "topic",
        "content",
        "action_label",
        "object_hint",
        "retention_detail",
    }
    assert len(generation.personal_positive_examples) == 5
    assert len(generation.personal_negative_examples) == 3
    assert generation.collected_writing_rules
    assert dict(generation.collected_template).keys() == {
        "summary_title",
        "summary_content",
        "summary_object_hint",
        "title",
        "content",
        "object_hint",
        "retention_detail",
    }
    assert len(generation.collected_positive_examples) == 2
    assert len(generation.collected_negative_examples) == 2
    assert event_generation_debug_summary(generation) == {
        "schema_version": 1,
        "config_loaded": True,
        "shared_writing_rule_count": 10,
        "personal_boundary_rule_count": 6,
        "personal_template_field_count": 5,
            "personal_positive_example_count": 5,
            "personal_negative_example_count": 3,
        "collected_writing_rule_count": 9,
        "collected_template_field_count": 7,
        "collected_positive_example_count": 2,
        "collected_negative_example_count": 2,
    }
    for source_value in (
        "陈之",
        "栗栋",
        "陈珏奇",
        "共享电单车9月需续保明细",
        "车辆编码.csv",
        "599辆",
        "1909个",
        "1298个",
    ):
        assert source_value not in raw_text


def test_event_generation_config_is_optional_for_isolated_environments(
    tmp_path: Path,
) -> None:
    config = load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)

    assert config.event_generation.shared_writing_rules == ()
    assert config.event_generation.personal_template == ()
    assert config.event_generation.collected_template == ()
    assert event_generation_debug_summary(config.event_generation) == {
        "schema_version": 1,
        "config_loaded": False,
        "shared_writing_rule_count": 0,
        "personal_boundary_rule_count": 0,
        "personal_template_field_count": 0,
        "personal_positive_example_count": 0,
        "personal_negative_example_count": 0,
        "collected_writing_rule_count": 0,
        "collected_template_field_count": 0,
        "collected_positive_example_count": 0,
        "collected_negative_example_count": 0,
    }


@pytest.mark.parametrize(
    "invalid_case",
    [
        "missing_field",
        "extra_field",
        "wrong_type",
        "wrong_version",
        "fractional_version",
        "empty_template",
        "empty_rules",
        "duplicate_example_name",
    ],
)
def test_load_runtime_config_rejects_invalid_event_generation_config(
    tmp_path: Path,
    invalid_case: str,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    payload = json.loads(
        Path("config/event_generation.json").read_text(encoding="utf-8")
    )
    if invalid_case == "missing_field":
        del payload["personal"]["template"]
    elif invalid_case == "extra_field":
        payload["collected"]["unexpected"] = []
    elif invalid_case == "wrong_type":
        payload["shared_writing_rules"] = "只写事实"
    elif invalid_case == "wrong_version":
        payload["schema_version"] = 2
    elif invalid_case == "fractional_version":
        payload["schema_version"] = 1.0
    elif invalid_case == "empty_template":
        payload["personal"]["template"]["topic"] = " "
    elif invalid_case == "empty_rules":
        payload["personal"]["event_boundary_rules"] = []
    elif invalid_case == "duplicate_example_name":
        payload["personal"]["negative_examples"][0]["name"] = payload[
            "personal"
        ]["positive_examples"][0]["name"]
    (config_dir / "event_generation.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Invalid event generation config"):
        load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)


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
                "day_group_validation_retry_limit": 1,
                "segmentation_retry_limit": 5,
                "event_extraction_retry_limit": 6,
                "stream_first_response_timeout_seconds": 61,
                "max_concurrent_llm_requests": 3,
                "max_concurrent_event_extraction_requests": 5,
                "max_concurrent_personal_fact_review_requests": 3,
                "max_concurrent_day_group_review_requests": 2,
                "codex_request_interval_min_seconds": 0,
                "codex_request_interval_max_seconds": 1,
                "max_concurrent_collected_merge_review_requests": 3,
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)

    assert config.online_request_retry_limit == 2
    assert config.day_group_validation_retry_limit == 1
    assert config.anchor_retry_limit == 5
    assert config.analysis_batch_retry_limit == 6
    assert config.stream_first_response_timeout_seconds == 61
    assert config.max_concurrent_llm_requests == 3
    assert config.max_concurrent_event_extraction_requests == 5
    assert config.max_concurrent_personal_fact_review_requests == 3
    assert config.max_concurrent_day_group_review_requests == 2
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
                "action_labels": [
                    {"key": "assigned", "label": "任务指派", "order": 20},
                    {"key": "decision_made", "label": "作出决策", "order": 10},
                ],
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
    assert [item.key for item in config.action_label_types] == [
        "decision_made",
        "assigned",
    ]
    assert [item.label for item in config.action_label_types] == [
        "作出决策",
        "任务指派",
    ]


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
    assert {item["key"] for item in payload["action_labels"]} >= {
        "assigned",
        "decision_made",
        "follow_up_assigned",
    }
    assert payload["manual_edit_field_label"] == "修订标记"
    assert [item["key"] for item in payload["manual_edit_types"]] == [
        "manual_added",
        "manual_modified",
        "manual_unknown",
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
                "review_same_conversation_only_groups": True,
                "review_semantic_only_object_conflicts": False,
                "review_broad_object_groups": False,
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
    assert payload["review_same_conversation_only_groups"] is True
    assert payload["review_semantic_only_object_conflicts"] is True
    assert payload["review_broad_object_groups"] is True
    grouping_payload = json.loads(
        Path("config/event_grouping.json").read_text(encoding="utf-8")
    )
    definitions = {
        item["key"]: item for item in grouping_payload["group_reason_definitions"]
    }
    assert grouping_payload["personal_grouping_negative_examples"]
    assert grouping_payload["personal_grouping_positive_examples"]
    assert grouping_payload["personal_group_discovery_rules"]
    assert grouping_payload["attachment_name_normalization"][
        "version_suffix_patterns"
    ]
    assert definitions["same_object"]["acceptance_rules"]
    assert definitions["same_object"]["rejection_rules"]
    assert definitions["continuous_action"]["acceptance_rules"]
    assert definitions["same_deliverable_batch"]["rejection_rules"]


def test_load_runtime_config_overrides_reads_self_delivery_config(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "self_delivery.json").write_text(
        json.dumps({"enabled": False}),
        encoding="utf-8",
    )

    config = load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)

    assert config.self_delivery_enabled is False


@pytest.mark.parametrize(
    "payload",
    [{}, {"enabled": "false"}, {"enabled": True, "other": False}],
)
def test_load_runtime_config_overrides_rejects_invalid_self_delivery_config(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "self_delivery.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Invalid self delivery config"):
        load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)


def test_repo_self_delivery_config_is_enabled() -> None:
    config = load_runtime_config_overrides(RuntimeConfig(), cwd=Path.cwd())

    assert config.self_delivery_enabled is True
    assert json.loads(Path("config/self_delivery.json").read_text(encoding="utf-8")) == {
        "enabled": True
    }


def test_load_runtime_config_rejects_invalid_attachment_version_pattern(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    payload = json.loads(
        Path("config/event_grouping.json").read_text(encoding="utf-8")
    )
    payload["attachment_name_normalization"]["version_suffix_patterns"] = ["("]
    (config_dir / "event_grouping.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="version suffix pattern is invalid"):
        load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)


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
            "review_same_conversation_only_groups": True,
            "review_semantic_only_object_conflicts": True,
            "review_broad_object_groups": True,
        },
        {
            "high_risk_review_enabled": "yes",
            "high_risk_source_event_count": 10,
            "high_risk_source_file_count": 4,
            "review_cross_batch_groups": True,
            "review_repaired_groups": True,
            "review_same_conversation_only_groups": True,
            "review_semantic_only_object_conflicts": True,
            "review_broad_object_groups": True,
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
    prompt_rules_text = "\n".join(policy.prompt_rules)
    assert "即使会议主题具体，也不要提炼为事件" in prompt_rules_text
    assert "忽略会议安排，只提炼有独立记录价值的业务内容" in prompt_rules_text
    assert (
        "之后没有实质反馈，或只有收到、已看、好的等简单确认时，不要提炼为事件"
        in prompt_rules_text
    )
    assert "已明确要求提交总结、审核结论、修改结果等具体产出" in prompt_rules_text
    assert "明确要求或确认执行合并、修改、校验、比对、去重、检查" in prompt_rules_text
    assert "对责任进行猜测或提出尚未采用的建议" in prompt_rules_text
    assert "已经完成的数据核查、问题排查或异常确认及其明确结论" in prompt_rules_text
    assert "这一排查事实与本人直接相关，必须提炼业务事件" in prompt_rules_text
    assert "不得忽略排查对象和结论" in prompt_rules_text
    assert "事件事实不依赖同消息中的配图或附件内容" in prompt_rules_text
    assert "不得请求读取配图或附件内容" in prompt_rules_text
    assert "不得因补读没有返回新内容而丢弃候选" in prompt_rules_text
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
                "day_group_validation_retry_limit": 1,
                "segmentation_retry_limit": 3,
                "event_extraction_retry_limit": 3,
                "stream_first_response_timeout_seconds": 60,
                "max_concurrent_llm_requests": 3,
                "max_concurrent_event_extraction_requests": 5,
                "max_concurrent_personal_fact_review_requests": 3,
                "max_concurrent_day_group_review_requests": 3,
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
                "day_group_validation_retry_limit": 1,
                "segmentation_retry_limit": 3,
                "event_extraction_retry_limit": 3,
                "stream_first_response_timeout_seconds": 60,
                "max_concurrent_llm_requests": 3,
                "max_concurrent_event_extraction_requests": 5,
                "max_concurrent_personal_fact_review_requests": 3,
                "max_concurrent_day_group_review_requests": 3,
                "codex_request_interval_min_seconds": 0,
                "codex_request_interval_max_seconds": 1,
                "max_concurrent_collected_merge_review_requests": 3,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="online_request_retry_limit"):
        load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)
