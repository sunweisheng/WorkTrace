from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping


DEFAULT_LLM_BASE_URL_ENV_VAR = "WORKTRACE_LLM_BASE_URL"
DEFAULT_LLM_MODEL_ENV_VAR = "WORKTRACE_LLM_MODEL"
DEFAULT_LLM_API_KEY_ENV_VAR = "WORKTRACE_LLM_API_KEY"
DEFAULT_LLM_TIMEOUT_ENV_VAR = "WORKTRACE_LLM_TIMEOUT_SECONDS"
DEFAULT_LLM_STREAM_ENV_VAR = "WORKTRACE_LLM_STREAM"
DEFAULT_LLM_TLS_VERIFY_ENV_VAR = "WORKTRACE_LLM_TLS_VERIFY"
DEFAULT_LLM_REASONING_EFFORT_ENV_VAR = "WORKTRACE_LLM_REASONING_EFFORT"
DEFAULT_COLLECTED_MERGE_TRACE_ENV_VAR = "WORKTRACE_COLLECTED_MERGE_TRACE"
DEFAULT_COLLECTED_MERGE_TRACE_ROOT_ENV_VAR = "WORKTRACE_COLLECTED_MERGE_TRACE_ROOT"
DEFAULT_COLLECTED_MERGE_RETRY_RATIO_ENV_VAR = (
    "WORKTRACE_COLLECTED_MERGE_MISSING_FIELD_RETRY_RATIO"
)
DEFAULT_COLLECTED_MERGE_RETRY_LIMIT_ENV_VAR = (
    "WORKTRACE_COLLECTED_MERGE_MISSING_FIELD_RETRY_LIMIT"
)
DEFAULT_LLM_ENV_FILE_NAME = ".env"
DEFAULT_EVENT_RULES_FILE_NAME = "config/event_rules.json"
DEFAULT_EVENT_METADATA_FILE_NAME = "config/event_metadata.json"
DEFAULT_REACTION_CATALOGS_ROOT = Path("config") / "reaction_catalogs"
DEFAULT_CONVERSATION_BLACKLIST_FILE_NAME = "config/conversation_blacklist.json"
DEFAULT_CONVERSATION_WINDOW_FILE_NAME = "config/conversation_window.json"
DEFAULT_LLM_RETRY_FILE_NAME = "config/llm_retry.json"
DEFAULT_EVENT_GROUPING_FILE_NAME = "config/event_grouping.json"
DEFAULT_COLLECTED_MERGE_FILE_NAME = "config/collected_merge.json"
DEFAULT_RETENTION_POLICY_FILE_NAME = "config/retention_policy.json"


@dataclass(frozen=True)
class OnlineLLMSettings:
    base_url: str
    model: str
    api_key: str
    timeout_seconds: int
    stream_first_response_timeout_seconds: int
    stream_enabled: bool
    tls_verify: bool
    reasoning_effort: str | None


@dataclass(frozen=True)
class EventMetadataItem:
    key: str
    label: str
    order: int


@dataclass(frozen=True)
class RetentionSignalDefinition:
    key: str
    description: str


@dataclass(frozen=True)
class CollectedGroupReasonDefinition:
    key: str
    description: str
    evidence_relation: str = ""
    supports_semantic_merge: bool = False
    acceptance_rules: tuple[str, ...] = ()
    rejection_rules: tuple[str, ...] = ()


DEFAULT_COLLECTED_GROUP_REASON_DEFINITIONS = (
    CollectedGroupReasonDefinition(
        key="shared_message",
        description="shared_message",
        evidence_relation="message",
    ),
    CollectedGroupReasonDefinition(
        key="shared_file",
        description="shared_file",
        evidence_relation="file",
    ),
    CollectedGroupReasonDefinition(
        key="same_conversation",
        description="same_conversation",
        evidence_relation="conversation",
    ),
    CollectedGroupReasonDefinition(
        key="same_object",
        description="same_object",
        supports_semantic_merge=True,
    ),
    CollectedGroupReasonDefinition(
        key="continuous_action",
        description="continuous_action",
        supports_semantic_merge=True,
    ),
    CollectedGroupReasonDefinition(
        key="same_deliverable_batch",
        description="same_deliverable_batch",
        supports_semantic_merge=True,
    ),
)


@dataclass(frozen=True)
class RetentionPolicyConfig:
    review_enabled: bool = False
    review_retention_reasons: tuple[str, ...] = ()
    require_no_referenced_files: bool = True
    uncertain_policy: str = "drop"
    prompt_rules: tuple[str, ...] = ()
    routine_signals: tuple[RetentionSignalDefinition, ...] = ()
    substantive_signals: tuple[RetentionSignalDefinition, ...] = ()
    fact_review_enabled: bool = False
    fact_review_source_message_count: int = 8
    fact_review_source_participant_count: int = 3
    fact_review_max_batch_candidates: int = 1
    fact_review_unsupported_policy: str = "drop"
    fact_review_rules: tuple[str, ...] = ()
    fact_risk_signals: tuple[RetentionSignalDefinition, ...] = ()
    generic_object_hints: tuple[str, ...] = ()
    personal_social_keywords: tuple[str, ...] = ()
    personal_leave_or_travel_keywords: tuple[str, ...] = ()
    personal_private_reason_keywords: tuple[str, ...] = ()
    personal_privacy_object_hints: tuple[str, ...] = ()
    generic_review_keywords: tuple[str, ...] = ()
    approval_action_keywords: tuple[str, ...] = ()
    administrative_approval_keywords: tuple[str, ...] = ()
    substantive_work_keywords: tuple[str, ...] = ()
    repeated_low_information_suffixes: tuple[str, ...] = ()


def parse_dotenv_lines(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def load_local_env_file(path: Path) -> dict[str, str]:
    try:
        return parse_dotenv_lines(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _read_local_env_values(
    config: RuntimeConfig,
    *,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    env_path = (cwd or Path.cwd()) / config.llm_env_file_name
    file_values = load_local_env_file(env_path)

    merged: dict[str, str] = dict(file_values)
    merged.update(env)
    return merged


def build_missing_llm_config_message(config: RuntimeConfig, missing_keys: list[str]) -> str:
    missing = ", ".join(missing_keys)
    return (
        "Missing online LLM configuration: "
        f"{missing}. WorkTrace requires the user to provide these values in local "
        f"`{config.llm_env_file_name}` or environment variables before running. "
        "Do not commit real secrets to git."
    )


def _parse_bool_value(raw_value: str, *, env_var: str) -> bool:
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid online LLM boolean: {env_var} must be true or false.")


def _parse_non_negative_float(raw_value: str, *, env_var: str) -> float:
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(
            f"Invalid online LLM delay: {env_var} must be a number."
        ) from exc
    if value < 0:
        raise ValueError(f"Invalid online LLM delay: {env_var} must be non-negative.")
    return value


def _parse_non_negative_int(raw_value: str, *, env_var: str) -> int:
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"Invalid integer config: {env_var} must be an integer.") from exc
    if value < 0:
        raise ValueError(f"Invalid integer config: {env_var} must be non-negative.")
    return value


def load_online_llm_settings(
    config: RuntimeConfig,
    *,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> OnlineLLMSettings:
    values = _read_local_env_values(config, cwd=cwd, environ=environ)
    required_keys = [
        config.llm_base_url_env_var,
        config.llm_model_env_var,
        config.llm_api_key_env_var,
    ]
    missing = [key for key in required_keys if not values.get(key, "").strip()]
    if missing:
        raise ValueError(build_missing_llm_config_message(config, missing))

    timeout_raw = values.get(config.llm_timeout_env_var, "").strip()
    timeout_seconds = config.analyzer_timeout_seconds
    if timeout_raw:
        try:
            timeout_seconds = int(timeout_raw)
        except ValueError as exc:
            raise ValueError(
                f"Invalid online LLM timeout: {config.llm_timeout_env_var} must be an integer."
            ) from exc
        if timeout_seconds <= 0:
            raise ValueError(
                f"Invalid online LLM timeout: {config.llm_timeout_env_var} must be positive."
            )

    stream_raw = values.get(config.llm_stream_env_var, "").strip()
    stream_enabled = config.llm_stream_enabled
    if stream_raw:
        stream_enabled = _parse_bool_value(stream_raw, env_var=config.llm_stream_env_var)

    tls_verify_raw = values.get(config.llm_tls_verify_env_var, "").strip()
    tls_verify = config.llm_tls_verify
    if tls_verify_raw:
        tls_verify = _parse_bool_value(
            tls_verify_raw,
            env_var=config.llm_tls_verify_env_var,
        )

    reasoning_effort_raw = values.get(config.llm_reasoning_effort_env_var, "").strip()
    reasoning_effort = reasoning_effort_raw or config.llm_reasoning_effort

    return OnlineLLMSettings(
        base_url=values[config.llm_base_url_env_var].strip(),
        model=values[config.llm_model_env_var].strip(),
        api_key=values[config.llm_api_key_env_var].strip(),
        timeout_seconds=timeout_seconds,
        stream_first_response_timeout_seconds=config.stream_first_response_timeout_seconds,
        stream_enabled=stream_enabled,
        tls_verify=tls_verify,
        reasoning_effort=reasoning_effort,
    )


def load_runtime_config_overrides(
    config: RuntimeConfig,
    *,
    cwd: Path | None = None,
) -> RuntimeConfig:
    base_dir = cwd or Path.cwd()
    config = _apply_runtime_env_overrides(config, cwd=base_dir)
    rules_path = base_dir / config.event_rules_file_name
    try:
        payload = json.loads(rules_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _load_supporting_config_overrides(config, base_dir=base_dir)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid event rules config: {rules_path} is not valid JSON."
        ) from exc

    if not isinstance(payload, dict):
        raise ValueError(
            f"Invalid event rules config: {rules_path} must contain a JSON object."
        )

    legacy_rule_keys = {
        "confidential_event_keywords",
        "non_work_sensitive_keywords",
        "excluded_event_topics",
        "excluded_event_content_signatures",
        "self_assignment_cues",
        "self_assignment_actions",
    }
    legacy_keys_found = sorted(legacy_rule_keys.intersection(payload))
    if legacy_keys_found:
        raise ValueError(
            "Invalid event rules config: legacy keys are no longer supported "
            f"({', '.join(legacy_keys_found)}). Use sensitive_event_keywords, "
            "excluded_event_keywords, and self_assignment_keywords."
        )

    supported_rule_keys = {
        "sensitive_event_keywords",
        "excluded_event_keywords",
        "self_assignment_keywords",
    }
    unexpected_rule_keys = sorted(set(payload).difference(supported_rule_keys))
    if unexpected_rule_keys:
        raise ValueError(
            "Invalid event rules config: unsupported keys "
            f"({', '.join(unexpected_rule_keys)})."
        )

    sensitive_event_keywords = _read_string_list(
        payload,
        key="sensitive_event_keywords",
        fallback=config.sensitive_event_keywords,
        file_path=rules_path,
    )
    excluded_event_keywords = _read_string_list(
        payload,
        key="excluded_event_keywords",
        fallback=config.excluded_event_keywords,
        file_path=rules_path,
    )
    self_assignment_keywords = _read_string_list(
        payload,
        key="self_assignment_keywords",
        fallback=config.self_assignment_keywords,
        file_path=rules_path,
    )
    if (
        sensitive_event_keywords == config.sensitive_event_keywords
        and excluded_event_keywords == config.excluded_event_keywords
        and self_assignment_keywords == config.self_assignment_keywords
    ):
        return _load_supporting_config_overrides(config, base_dir=base_dir)

    config = replace(
        config,
        sensitive_event_keywords=sensitive_event_keywords,
        excluded_event_keywords=excluded_event_keywords,
        self_assignment_keywords=self_assignment_keywords,
    )
    return _load_supporting_config_overrides(config, base_dir=base_dir)


def _load_supporting_config_overrides(
    config: RuntimeConfig,
    *,
    base_dir: Path,
) -> RuntimeConfig:
    config = _load_retention_policy_overrides(config, base_dir=base_dir)
    config = _load_event_metadata_overrides(config, base_dir=base_dir)
    config = _load_conversation_window_overrides(config, base_dir=base_dir)
    config = _load_llm_retry_overrides(config, base_dir=base_dir)
    config = _load_event_grouping_overrides(config, base_dir=base_dir)
    return _load_collected_merge_overrides(config, base_dir=base_dir)


def _load_retention_policy_overrides(
    config: RuntimeConfig,
    *,
    base_dir: Path,
) -> RuntimeConfig:
    config_path = base_dir / config.retention_policy_file_name
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid retention policy config: {config_path} is not valid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(
            f"Invalid retention policy config: {config_path} must contain a JSON object."
        )

    expected_keys = {
        "review",
        "prompt_rules",
        "routine_signals",
        "substantive_signals",
        "fact_review",
        "fact_review_rules",
        "fact_risk_signals",
        "generic_object_hints",
        "personal_social_keywords",
        "personal_leave_or_travel_keywords",
        "personal_private_reason_keywords",
        "personal_privacy_object_hints",
        "generic_review_keywords",
        "approval_action_keywords",
        "administrative_approval_keywords",
        "substantive_work_keywords",
        "repeated_low_information_suffixes",
    }
    unexpected = sorted(set(payload).difference(expected_keys))
    missing = sorted(expected_keys.difference(payload))
    if unexpected or missing:
        details: list[str] = []
        if unexpected:
            details.append(f"unsupported keys {', '.join(unexpected)}")
        if missing:
            details.append(f"missing keys {', '.join(missing)}")
        raise ValueError(
            "Invalid retention policy config: " + "; ".join(details) + "."
        )

    review = payload["review"]
    if not isinstance(review, dict):
        raise ValueError(
            "Invalid retention policy config: `review` must be an object."
        )
    review_keys = {
        "enabled",
        "retention_reasons",
        "require_no_referenced_files",
        "uncertain_policy",
    }
    review_unexpected = sorted(set(review).difference(review_keys))
    review_missing = sorted(review_keys.difference(review))
    if review_unexpected or review_missing:
        raise ValueError(
            "Invalid retention policy config: `review` fields do not match the contract."
        )
    bool_keys = {
        "enabled",
        "require_no_referenced_files",
    }
    if any(not isinstance(review[key], bool) for key in bool_keys):
        raise ValueError(
            "Invalid retention policy config: review switches must be booleans."
        )
    uncertain_policy = review["uncertain_policy"]
    if uncertain_policy not in {"drop", "keep"}:
        raise ValueError(
            "Invalid retention policy config: uncertain_policy must be drop or keep."
        )

    fact_review = payload["fact_review"]
    if not isinstance(fact_review, dict):
        raise ValueError(
            "Invalid retention policy config: `fact_review` must be an object."
        )
    fact_review_keys = {
        "enabled",
        "source_message_count",
        "source_participant_count",
        "max_batch_candidates",
        "unsupported_policy",
    }
    if set(fact_review) != fact_review_keys:
        raise ValueError(
            "Invalid retention policy config: `fact_review` fields do not match the contract."
        )
    if not isinstance(fact_review["enabled"], bool):
        raise ValueError(
            "Invalid retention policy config: fact_review.enabled must be a boolean."
        )
    for key in (
        "source_message_count",
        "source_participant_count",
        "max_batch_candidates",
    ):
        value = fact_review[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(
                "Invalid retention policy config: "
                f"fact_review.{key} must be a positive integer."
            )
    fact_review_unsupported_policy = fact_review["unsupported_policy"]
    if fact_review_unsupported_policy not in {"drop", "fail"}:
        raise ValueError(
            "Invalid retention policy config: "
            "fact_review.unsupported_policy must be drop or fail."
        )

    policy = RetentionPolicyConfig(
        review_enabled=review["enabled"],
        review_retention_reasons=_read_string_list(
            review,
            key="retention_reasons",
            fallback=(),
            file_path=config_path,
            error_prefix="Invalid retention policy config",
        ),
        require_no_referenced_files=review["require_no_referenced_files"],
        uncertain_policy=uncertain_policy,
        prompt_rules=_read_string_list(
            payload,
            key="prompt_rules",
            fallback=(),
            file_path=config_path,
            error_prefix="Invalid retention policy config",
        ),
        routine_signals=_read_retention_signal_definitions(
            payload["routine_signals"],
            config_path=config_path,
            field_name="routine_signals",
        ),
        substantive_signals=_read_retention_signal_definitions(
            payload["substantive_signals"],
            config_path=config_path,
            field_name="substantive_signals",
        ),
        fact_review_enabled=fact_review["enabled"],
        fact_review_source_message_count=fact_review["source_message_count"],
        fact_review_source_participant_count=fact_review[
            "source_participant_count"
        ],
        fact_review_max_batch_candidates=fact_review["max_batch_candidates"],
        fact_review_unsupported_policy=fact_review_unsupported_policy,
        fact_review_rules=_read_string_list(
            payload,
            key="fact_review_rules",
            fallback=(),
            file_path=config_path,
            error_prefix="Invalid retention policy config",
        ),
        fact_risk_signals=_read_retention_signal_definitions(
            payload["fact_risk_signals"],
            config_path=config_path,
            field_name="fact_risk_signals",
        ),
        generic_object_hints=_read_retention_policy_list(
            payload, "generic_object_hints", config_path
        ),
        personal_social_keywords=_read_retention_policy_list(
            payload, "personal_social_keywords", config_path
        ),
        personal_leave_or_travel_keywords=_read_retention_policy_list(
            payload, "personal_leave_or_travel_keywords", config_path
        ),
        personal_private_reason_keywords=_read_retention_policy_list(
            payload, "personal_private_reason_keywords", config_path
        ),
        personal_privacy_object_hints=_read_retention_policy_list(
            payload, "personal_privacy_object_hints", config_path
        ),
        generic_review_keywords=_read_retention_policy_list(
            payload, "generic_review_keywords", config_path
        ),
        approval_action_keywords=_read_retention_policy_list(
            payload, "approval_action_keywords", config_path
        ),
        administrative_approval_keywords=_read_retention_policy_list(
            payload, "administrative_approval_keywords", config_path
        ),
        substantive_work_keywords=_read_retention_policy_list(
            payload, "substantive_work_keywords", config_path
        ),
        repeated_low_information_suffixes=_read_retention_policy_list(
            payload, "repeated_low_information_suffixes", config_path
        ),
    )
    if not policy.review_retention_reasons:
        raise ValueError(
            "Invalid retention policy config: review retention_reasons cannot be empty."
        )
    routine_keys = {item.key for item in policy.routine_signals}
    substantive_keys = {item.key for item in policy.substantive_signals}
    if not routine_keys or not substantive_keys or routine_keys & substantive_keys:
        raise ValueError(
            "Invalid retention policy config: signal keys must be non-empty and distinct."
        )
    if policy.fact_review_enabled and (
        not policy.fact_review_rules or not policy.fact_risk_signals
    ):
        raise ValueError(
            "Invalid retention policy config: enabled fact review requires rules and risk signals."
        )
    return replace(config, retention_policy=policy)


def _read_retention_policy_list(
    payload: dict[str, object],
    key: str,
    config_path: Path,
) -> tuple[str, ...]:
    return _read_string_list(
        payload,
        key=key,
        fallback=(),
        file_path=config_path,
        error_prefix="Invalid retention policy config",
    )


def _read_retention_signal_definitions(
    raw_value: object,
    *,
    config_path: Path,
    field_name: str,
) -> tuple[RetentionSignalDefinition, ...]:
    if not isinstance(raw_value, list):
        raise ValueError(
            "Invalid retention policy config: "
            f"{config_path} field `{field_name}` must be a list."
        )
    definitions: list[RetentionSignalDefinition] = []
    seen: set[str] = set()
    for item in raw_value:
        if not isinstance(item, dict) or set(item) != {"key", "description"}:
            raise ValueError(
                "Invalid retention policy config: "
                f"`{field_name}` items must contain key and description."
            )
        key = item["key"]
        description = item["description"]
        if not isinstance(key, str) or not isinstance(description, str):
            raise ValueError(
                "Invalid retention policy config: "
                f"`{field_name}` values must be strings."
            )
        key = key.strip()
        description = description.strip()
        if not key or not description or key in seen:
            raise ValueError(
                "Invalid retention policy config: "
                f"`{field_name}` contains an empty or duplicate key."
            )
        seen.add(key)
        definitions.append(RetentionSignalDefinition(key=key, description=description))
    return tuple(definitions)


def _load_collected_merge_overrides(
    config: RuntimeConfig,
    *,
    base_dir: Path,
) -> RuntimeConfig:
    config_path = base_dir / config.collected_merge_file_name
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid collected merge config: {config_path} is not valid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(
            f"Invalid collected merge config: {config_path} must contain a JSON object."
        )

    bool_keys = {
        "high_risk_review_enabled",
        "review_cross_batch_groups",
        "review_repaired_groups",
        "review_same_conversation_only_groups",
        "review_semantic_only_object_conflicts",
        "review_broad_object_groups",
    }
    int_keys = {
        "high_risk_source_event_count",
        "high_risk_source_file_count",
    }
    keys = bool_keys | int_keys
    unexpected = sorted(set(payload).difference(keys))
    missing = sorted(keys.difference(payload))
    if unexpected or missing:
        details = []
        if unexpected:
            details.append(f"unsupported keys {', '.join(unexpected)}")
        if missing:
            details.append(f"missing keys {', '.join(missing)}")
        raise ValueError(f"Invalid collected merge config: {'; '.join(details)}.")

    values: dict[str, object] = {}
    for key in bool_keys:
        value = payload[key]
        if not isinstance(value, bool):
            raise ValueError(
                f"Invalid collected merge config: {config_path} field `{key}` "
                "must be a boolean."
            )
        values[key] = value
    for key in int_keys:
        value = payload[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(
                f"Invalid collected merge config: {config_path} field `{key}` "
                "must be a positive integer."
            )
        values[key] = value
    return replace(config, **values)


def _load_event_grouping_overrides(
    config: RuntimeConfig,
    *,
    base_dir: Path,
) -> RuntimeConfig:
    config_path = base_dir / config.event_grouping_file_name
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid event grouping config: {config_path} is not valid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(
            f"Invalid event grouping config: {config_path} must contain a JSON object."
        )
    expected_keys = {
        "personal_grouping_rules",
        "personal_review_rules",
        "group_reason_definitions",
    }
    if set(payload) != expected_keys:
        raise ValueError(
            "Invalid event grouping config: fields do not match the contract."
        )
    grouping_rules = _read_string_list(
        payload,
        key="personal_grouping_rules",
        fallback=(),
        file_path=config_path,
        error_prefix="Invalid event grouping config",
    )
    review_rules = _read_string_list(
        payload,
        key="personal_review_rules",
        fallback=(),
        file_path=config_path,
        error_prefix="Invalid event grouping config",
    )
    if not grouping_rules or not review_rules:
        raise ValueError(
            "Invalid event grouping config: personal rules must not be empty."
        )
    definitions = _read_collected_group_reason_definitions(
        payload["group_reason_definitions"],
        config_path=config_path,
        error_prefix="Invalid event grouping config",
    )
    return replace(
        config,
        personal_grouping_rules=grouping_rules,
        personal_group_review_rules=review_rules,
        collected_group_reason_definitions=definitions,
    )


def _read_collected_group_reason_definitions(
    raw_value: object,
    *,
    config_path: Path,
    error_prefix: str = "Invalid collected merge config",
) -> tuple[CollectedGroupReasonDefinition, ...]:
    if not isinstance(raw_value, list) or not raw_value:
        raise ValueError(
            f"{error_prefix}: "
            f"{config_path} field `group_reason_definitions` must be a non-empty list."
        )
    definitions: list[CollectedGroupReasonDefinition] = []
    seen: set[str] = set()
    expected_keys = {
        "key",
        "description",
        "evidence_relation",
        "supports_semantic_merge",
        "acceptance_rules",
        "rejection_rules",
    }
    supported_relations = {"", "message", "file", "conversation"}
    for item in raw_value:
        if not isinstance(item, dict) or set(item) != expected_keys:
            raise ValueError(
                f"{error_prefix}: `group_reason_definitions` items "
                "must contain key, description, evidence_relation, "
                "supports_semantic_merge, acceptance_rules and rejection_rules."
            )
        key = item["key"]
        description = item["description"]
        evidence_relation = item["evidence_relation"]
        supports_semantic_merge = item["supports_semantic_merge"]
        acceptance_rules = item["acceptance_rules"]
        rejection_rules = item["rejection_rules"]
        if not all(
            isinstance(value, str)
            for value in (key, description, evidence_relation)
        ) or not isinstance(supports_semantic_merge, bool):
            raise ValueError(
                f"{error_prefix}: group reason values have invalid types."
            )
        if not all(
            isinstance(values, list)
            and all(isinstance(value, str) and value.strip() for value in values)
            for values in (acceptance_rules, rejection_rules)
        ):
            raise ValueError(
                "Invalid collected merge config: group reason rules must be lists "
                "of non-empty strings."
            )
        key = key.strip()
        description = description.strip()
        evidence_relation = evidence_relation.strip()
        if (
            not key
            or not description
            or key in seen
            or evidence_relation not in supported_relations
            or (evidence_relation and supports_semantic_merge)
        ):
            raise ValueError(
                "Invalid collected merge config: group reason definitions contain "
                "an empty, duplicate or conflicting value."
            )
        seen.add(key)
        definitions.append(
            CollectedGroupReasonDefinition(
                key=key,
                description=description,
                evidence_relation=evidence_relation,
                supports_semantic_merge=supports_semantic_merge,
                acceptance_rules=tuple(value.strip() for value in acceptance_rules),
                rejection_rules=tuple(value.strip() for value in rejection_rules),
            )
        )
    return tuple(definitions)


def _load_conversation_window_overrides(
    config: RuntimeConfig,
    *,
    base_dir: Path,
) -> RuntimeConfig:
    window_path = base_dir / config.conversation_window_file_name
    try:
        payload = json.loads(window_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid conversation window config: {window_path} is not valid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid conversation window config: {window_path} must contain a JSON object.")
    keys = {
        "max_anchor_gap_minutes",
        "max_unrelated_intervening_messages",
        "initial_context_messages_before",
        "context_expansion_messages_per_direction",
        "context_expansion_round_limit",
    }
    unexpected = sorted(set(payload).difference(keys))
    missing = sorted(keys.difference(payload))
    if unexpected or missing:
        details = []
        if unexpected:
            details.append(f"unsupported keys {', '.join(unexpected)}")
        if missing:
            details.append(f"missing keys {', '.join(missing)}")
        raise ValueError(f"Invalid conversation window config: {'; '.join(details)}.")
    values: dict[str, int] = {}
    for key in keys:
        value = payload[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(
                f"Invalid conversation window config: {window_path} field `{key}` must be a non-negative integer."
            )
        values[key] = value
    if values["context_expansion_messages_per_direction"] < 1:
        raise ValueError("Invalid conversation window config: context_expansion_messages_per_direction must be positive.")
    return replace(config, **values)


def _load_llm_retry_overrides(
    config: RuntimeConfig,
    *,
    base_dir: Path,
) -> RuntimeConfig:
    retry_path = base_dir / config.llm_retry_file_name
    try:
        payload = json.loads(retry_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid LLM retry config: {retry_path} is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid LLM retry config: {retry_path} must contain a JSON object.")
    keys = {
        "online_request_retry_limit",
        "day_group_validation_retry_limit",
        "segmentation_retry_limit",
        "event_extraction_retry_limit",
        "stream_first_response_timeout_seconds",
        "max_concurrent_llm_requests",
        "max_concurrent_event_extraction_requests",
        "max_concurrent_personal_fact_review_requests",
        "max_concurrent_day_group_review_requests",
        "codex_request_interval_min_seconds",
        "codex_request_interval_max_seconds",
        "max_concurrent_collected_merge_review_requests",
    }
    unexpected = sorted(set(payload).difference(keys))
    missing = sorted(keys.difference(payload))
    if unexpected or missing:
        details = []
        if unexpected:
            details.append(f"unsupported keys {', '.join(unexpected)}")
        if missing:
            details.append(f"missing keys {', '.join(missing)}")
        raise ValueError(f"Invalid LLM retry config: {'; '.join(details)}.")
    values: dict[str, int | float] = {}
    for key in keys:
        value = payload[key]
        if key in {
            "codex_request_interval_min_seconds",
            "codex_request_interval_max_seconds",
        }:
            if (
                not isinstance(value, int | float)
                or isinstance(value, bool)
                or value < 0
            ):
                raise ValueError(
                    f"Invalid LLM retry config: {retry_path} field `{key}` must be non-negative."
                )
            values[key] = float(value)
            continue
        minimum = (
            1
            if key
            in {
                "stream_first_response_timeout_seconds",
                "max_concurrent_personal_fact_review_requests",
                "max_concurrent_day_group_review_requests",
                "max_concurrent_collected_merge_review_requests",
            }
            else 0
        )
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ValueError(
                f"Invalid LLM retry config: {retry_path} field `{key}` must be at least {minimum}."
            )
        values[key] = value
    if (
        values["codex_request_interval_min_seconds"]
        > values["codex_request_interval_max_seconds"]
    ):
        raise ValueError(
            "Invalid LLM retry config: codex_request_interval_min_seconds must be "
            "less than or equal to codex_request_interval_max_seconds."
        )
    return replace(
        config,
        online_request_retry_limit=values["online_request_retry_limit"],
        anchor_retry_limit=values["segmentation_retry_limit"],
        analysis_batch_retry_limit=values["event_extraction_retry_limit"],
        stream_first_response_timeout_seconds=values["stream_first_response_timeout_seconds"],
        max_concurrent_llm_requests=values["max_concurrent_llm_requests"],
        max_concurrent_event_extraction_requests=values[
            "max_concurrent_event_extraction_requests"
        ],
        max_concurrent_personal_fact_review_requests=values[
            "max_concurrent_personal_fact_review_requests"
        ],
        day_group_validation_retry_limit=values["day_group_validation_retry_limit"],
        max_concurrent_day_group_review_requests=values[
            "max_concurrent_day_group_review_requests"
        ],
        codex_request_interval_min_seconds=values[
            "codex_request_interval_min_seconds"
        ],
        codex_request_interval_max_seconds=values[
            "codex_request_interval_max_seconds"
        ],
        max_concurrent_collected_merge_review_requests=values[
            "max_concurrent_collected_merge_review_requests"
        ],
    )


def _load_event_metadata_overrides(
    config: RuntimeConfig,
    *,
    base_dir: Path,
) -> RuntimeConfig:
    metadata_path = base_dir / config.event_metadata_file_name
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid event metadata config: {metadata_path} is not valid JSON."
        ) from exc

    if not isinstance(payload, dict):
        raise ValueError(
            f"Invalid event metadata config: {metadata_path} must contain a JSON object."
        )
    unexpected_keys = sorted(set(payload).difference({"self_relations"}))
    if unexpected_keys:
        raise ValueError(
            "Invalid event metadata config: unsupported keys "
            f"({', '.join(unexpected_keys)})."
        )

    raw_items = payload.get("self_relations", [])
    if not isinstance(raw_items, list):
        raise ValueError(
            f"Invalid event metadata config: {metadata_path} field `self_relations` must be a list."
        )

    items: list[EventMetadataItem] = []
    seen_keys: set[str] = set()
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise ValueError(
                f"Invalid event metadata config: {metadata_path} self relation entries must be objects."
            )
        if set(raw_item) != {"key", "label", "order"}:
            raise ValueError(
                f"Invalid event metadata config: {metadata_path} self relation entries require key, label, and order."
            )
        key = raw_item.get("key")
        label = raw_item.get("label")
        order = raw_item.get("order")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Invalid event metadata config: self relation key must be non-empty.")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("Invalid event metadata config: self relation label must be non-empty.")
        if not isinstance(order, int) or isinstance(order, bool):
            raise ValueError("Invalid event metadata config: self relation order must be an integer.")
        cleaned_key = key.strip()
        if cleaned_key in seen_keys:
            raise ValueError(
                f"Invalid event metadata config: duplicate self relation key `{cleaned_key}`."
            )
        seen_keys.add(cleaned_key)
        items.append(
            EventMetadataItem(
                key=cleaned_key,
                label=label.strip(),
                order=order,
            )
        )

    return replace(
        config,
        self_relation_types=tuple(sorted(items, key=lambda item: (item.order, item.key))),
    )


def _apply_runtime_env_overrides(
    config: RuntimeConfig,
    *,
    cwd: Path | None = None,
) -> RuntimeConfig:
    values = _read_local_env_values(config, cwd=cwd)
    updates: dict[str, object] = {}

    trace_raw = values.get(config.collected_merge_trace_env_var, "").strip()
    if trace_raw:
        updates["collected_merge_trace_enabled"] = _parse_bool_value(
            trace_raw,
            env_var=config.collected_merge_trace_env_var,
        )

    trace_root_raw = values.get(config.collected_merge_trace_root_env_var, "").strip()
    if trace_root_raw:
        updates["collected_merge_trace_root"] = Path(trace_root_raw)

    retry_ratio_raw = values.get(config.collected_merge_retry_ratio_env_var, "").strip()
    if retry_ratio_raw:
        retry_ratio = _parse_non_negative_float(
            retry_ratio_raw,
            env_var=config.collected_merge_retry_ratio_env_var,
        )
        if retry_ratio > 1:
            raise ValueError(
                "Invalid collected merge retry ratio: "
                f"{config.collected_merge_retry_ratio_env_var} must be between 0 and 1."
            )
        updates["collected_merge_missing_field_retry_ratio"] = retry_ratio

    retry_limit_raw = values.get(config.collected_merge_retry_limit_env_var, "").strip()
    if retry_limit_raw:
        updates["collected_merge_missing_field_retry_limit"] = _parse_non_negative_int(
            retry_limit_raw,
            env_var=config.collected_merge_retry_limit_env_var,
        )

    if not updates:
        return config
    return replace(config, **updates)


def load_conversation_blacklist_overrides(
    config: RuntimeConfig,
    *,
    cwd: Path | None = None,
) -> RuntimeConfig:
    blacklist_path = (cwd or Path.cwd()) / config.conversation_blacklist_file_name
    try:
        payload = json.loads(blacklist_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid conversation blacklist config: {blacklist_path} is not valid JSON."
        ) from exc

    if not isinstance(payload, dict):
        raise ValueError(
            f"Invalid conversation blacklist config: {blacklist_path} must contain a JSON object."
        )

    excluded_conversation_ids = _read_string_list(
        payload,
        key="excluded_conversation_ids",
        fallback=config.excluded_conversation_ids,
        file_path=blacklist_path,
        error_prefix="Invalid conversation blacklist config",
    )

    deduped_ids = tuple(dict.fromkeys(excluded_conversation_ids))
    if deduped_ids == config.excluded_conversation_ids:
        return config

    return replace(config, excluded_conversation_ids=deduped_ids)


def _read_string_list(
    payload: dict[str, object],
    *,
    key: str,
    fallback: tuple[str, ...],
    file_path: Path,
    error_prefix: str = "Invalid event rules config",
) -> tuple[str, ...]:
    raw_value = payload.get(key)
    if raw_value is None:
        return fallback
    if not isinstance(raw_value, list):
        raise ValueError(
            f"{error_prefix}: {file_path} field `{key}` must be a list."
        )
    values: list[str] = []
    for item in raw_value:
        if not isinstance(item, str):
            raise ValueError(
                f"{error_prefix}: {file_path} field `{key}` must contain only strings."
            )
        cleaned = item.strip()
        if cleaned:
            values.append(cleaned)
    return tuple(values)


@dataclass(frozen=True)
class RuntimeConfig:
    timezone: str = "Asia/Shanghai"
    analyzer_backend: str = "online"
    online_request_retry_limit: int = 1
    day_group_validation_retry_limit: int = 1
    anchor_retry_limit: int = 3
    analysis_batch_retry_limit: int = 3
    stream_first_response_timeout_seconds: int = 60
    max_concurrent_llm_requests: int = 1
    max_concurrent_event_extraction_requests: int | None = None
    max_concurrent_personal_fact_review_requests: int = 1
    max_concurrent_day_group_review_requests: int = 3
    max_concurrent_collected_merge_review_requests: int = 3
    codex_request_interval_min_seconds: float = 0.0
    codex_request_interval_max_seconds: float = 1.0
    anchor_batch_retry_limit: int = 1
    conversation_segmentation_failure_threshold: int = 2
    reaction_discovery_page_limit: int = 3
    slice_base_limit: int = 150
    model_input_batch_target_tokens: int = 5200
    collected_merge_missing_field_retry_ratio: float = 0.2
    collected_merge_missing_field_retry_limit: int = 1
    collected_merge_trace_enabled: bool = False
    collected_merge_trace_root: Path = field(
        default_factory=lambda: Path("data") / "debug" / "collected_merge"
    )
    high_risk_review_enabled: bool = True
    high_risk_source_event_count: int = 10
    high_risk_source_file_count: int = 4
    review_cross_batch_groups: bool = True
    review_repaired_groups: bool = True
    review_same_conversation_only_groups: bool = True
    review_semantic_only_object_conflicts: bool = True
    review_broad_object_groups: bool = True
    collected_group_reason_definitions: tuple[
        CollectedGroupReasonDefinition, ...
    ] = DEFAULT_COLLECTED_GROUP_REASON_DEFINITIONS
    personal_grouping_rules: tuple[str, ...] = ()
    personal_group_review_rules: tuple[str, ...] = ()
    slice_retry_limit: int = 3
    prompt_slice_message_limit: int = 40
    prompt_message_char_limit: int = 300
    prompt_attachment_char_limit: int = 800
    prompt_time_format: str = "%H:%M"
    max_anchor_gap_minutes: int = 10
    max_unrelated_intervening_messages: int = 3
    initial_context_messages_before: int = 2
    context_expansion_messages_per_direction: int = 7
    context_expansion_round_limit: int = 2
    use_initial_conversation_windows: bool = True
    analyzer_timeout_seconds: int = 180
    codex_stdin_mode: bool = False
    anchor_batch_size: int = 3
    sensitive_event_keywords: tuple[str, ...] = ()
    excluded_event_keywords: tuple[str, ...] = ()
    self_assignment_keywords: tuple[str, ...] = ()
    self_relation_types: tuple[EventMetadataItem, ...] = ()
    retention_policy: RetentionPolicyConfig = field(default_factory=RetentionPolicyConfig)
    reaction_catalogs_root: Path = DEFAULT_REACTION_CATALOGS_ROOT
    excluded_conversation_ids: tuple[str, ...] = ()
    data_root: Path = field(default_factory=lambda: Path("data"))
    cache_root: Path | None = None
    conversation_debug_root: Path | None = None
    generator_name: str = "worktrace"
    llm_base_url_env_var: str = DEFAULT_LLM_BASE_URL_ENV_VAR
    llm_model_env_var: str = DEFAULT_LLM_MODEL_ENV_VAR
    llm_api_key_env_var: str = DEFAULT_LLM_API_KEY_ENV_VAR
    llm_timeout_env_var: str = DEFAULT_LLM_TIMEOUT_ENV_VAR
    llm_stream_env_var: str = DEFAULT_LLM_STREAM_ENV_VAR
    llm_tls_verify_env_var: str = DEFAULT_LLM_TLS_VERIFY_ENV_VAR
    llm_reasoning_effort_env_var: str = DEFAULT_LLM_REASONING_EFFORT_ENV_VAR
    collected_merge_trace_env_var: str = DEFAULT_COLLECTED_MERGE_TRACE_ENV_VAR
    collected_merge_trace_root_env_var: str = DEFAULT_COLLECTED_MERGE_TRACE_ROOT_ENV_VAR
    collected_merge_retry_ratio_env_var: str = DEFAULT_COLLECTED_MERGE_RETRY_RATIO_ENV_VAR
    collected_merge_retry_limit_env_var: str = DEFAULT_COLLECTED_MERGE_RETRY_LIMIT_ENV_VAR
    llm_env_file_name: str = DEFAULT_LLM_ENV_FILE_NAME
    event_rules_file_name: str = DEFAULT_EVENT_RULES_FILE_NAME
    event_metadata_file_name: str = DEFAULT_EVENT_METADATA_FILE_NAME
    conversation_blacklist_file_name: str = DEFAULT_CONVERSATION_BLACKLIST_FILE_NAME
    conversation_window_file_name: str = DEFAULT_CONVERSATION_WINDOW_FILE_NAME
    llm_retry_file_name: str = DEFAULT_LLM_RETRY_FILE_NAME
    event_grouping_file_name: str = DEFAULT_EVENT_GROUPING_FILE_NAME
    collected_merge_file_name: str = DEFAULT_COLLECTED_MERGE_FILE_NAME
    retention_policy_file_name: str = DEFAULT_RETENTION_POLICY_FILE_NAME
    llm_stream_enabled: bool = False
    llm_tls_verify: bool = False
    llm_reasoning_effort: str | None = "none"


DEFAULT_CONFIG = RuntimeConfig()
