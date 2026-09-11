from __future__ import annotations

import json
import os
import re
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
DEFAULT_CODEX_MODEL_ENV_VAR = "WORKTRACE_CODEX_MODEL"
DEFAULT_CODEX_REASONING_EFFORT_ENV_VAR = "WORKTRACE_CODEX_REASONING_EFFORT"
DEFAULT_CODEX_PROVIDER_ID_ENV_VAR = "WORKTRACE_CODEX_PROVIDER_ID"
DEFAULT_CODEX_PROVIDER_NAME_ENV_VAR = "WORKTRACE_CODEX_PROVIDER_NAME"
DEFAULT_CODEX_PROVIDER_BASE_URL_ENV_VAR = "WORKTRACE_CODEX_PROVIDER_BASE_URL"
DEFAULT_CODEX_PROVIDER_WIRE_API_ENV_VAR = "WORKTRACE_CODEX_PROVIDER_WIRE_API"
DEFAULT_CODEX_PROVIDER_REQUIRES_OPENAI_AUTH_ENV_VAR = (
    "WORKTRACE_CODEX_PROVIDER_REQUIRES_OPENAI_AUTH"
)
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
DEFAULT_EVENT_GENERATION_FILE_NAME = "config/event_generation.json"
DEFAULT_MODEL_INPUT_BUDGET_FILE_NAME = "config/model_input_budget.json"
DEFAULT_COLLECTED_MERGE_FILE_NAME = "config/collected_merge.json"
DEFAULT_RETENTION_POLICY_FILE_NAME = "config/retention_policy.json"
DEFAULT_SELF_DELIVERY_FILE_NAME = "config/self_delivery.json"


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
class CodexLLMSettings:
    model: str
    reasoning_effort: str
    provider_id: str
    provider_name: str
    provider_base_url: str
    provider_wire_api: str
    provider_requires_openai_auth: bool
    timeout_seconds: int


@dataclass(frozen=True)
class EventMetadataItem:
    key: str
    label: str
    order: int


DEFAULT_MANUAL_EDIT_TYPES = (
    EventMetadataItem("manual_added", "manual_added", 10),
    EventMetadataItem("manual_modified", "manual_modified", 20),
    EventMetadataItem("manual_unknown", "manual_unknown", 30),
)


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


@dataclass(frozen=True)
class EventGenerationExample:
    name: str
    source_facts: tuple[str, ...]
    expected_output: tuple[tuple[str, str], ...]

    def to_prompt_dict(self, *, fields: tuple[str, ...] | None = None) -> dict[str, object]:
        allowed_fields = set(fields or ())
        expected_output = {
            key: value
            for key, value in self.expected_output
            if not allowed_fields or key in allowed_fields
        }
        return {
            "name": self.name,
            "source_facts": list(self.source_facts),
            "expected_output": expected_output,
        }


@dataclass(frozen=True)
class EventGenerationNegativeExample:
    name: str
    source_facts: tuple[str, ...]
    incorrect_behavior: str
    expected_behavior: str

    def to_prompt_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "source_facts": list(self.source_facts),
            "incorrect_behavior": self.incorrect_behavior,
            "expected_behavior": self.expected_behavior,
        }


@dataclass(frozen=True)
class EventGenerationConfig:
    schema_version: int = 1
    shared_writing_rules: tuple[str, ...] = ()
    personal_event_boundary_rules: tuple[str, ...] = ()
    personal_template: tuple[tuple[str, str], ...] = ()
    personal_positive_examples: tuple[EventGenerationExample, ...] = ()
    personal_negative_examples: tuple[EventGenerationNegativeExample, ...] = ()
    collected_writing_rules: tuple[str, ...] = ()
    collected_template: tuple[tuple[str, str], ...] = ()
    collected_positive_examples: tuple[EventGenerationExample, ...] = ()
    collected_negative_examples: tuple[EventGenerationNegativeExample, ...] = ()


@dataclass(frozen=True)
class ModelInputBudgetProfile:
    profile_id: str
    primary_model: str
    fallback_model: str
    target_tokens: int
    benchmark_dataset_version: str


@dataclass(frozen=True)
class ModelInputBudgetConfig:
    schema_version: int = 1
    default_target_tokens: int = 7000
    profiles: tuple[ModelInputBudgetProfile, ...] = ()


@dataclass(frozen=True)
class ModelInputBudgetSelection:
    profile_matched: bool = False
    profile_id: str = ""
    target_tokens: int = 7000
    benchmark_dataset_version: str = ""


def model_input_budget_debug_summary(
    selection: ModelInputBudgetSelection,
) -> dict[str, bool | int | str]:
    return {
        "profile_matched": selection.profile_matched,
        "profile_id": selection.profile_id or "default",
        "target_tokens": selection.target_tokens,
        "benchmark_dataset_version": (
            selection.benchmark_dataset_version or "not_available"
        ),
    }


def event_generation_debug_summary(
    config: EventGenerationConfig,
) -> dict[str, int | bool]:
    config_loaded = all(
        (
            config.shared_writing_rules,
            config.personal_event_boundary_rules,
            config.personal_template,
            config.personal_positive_examples,
            config.personal_negative_examples,
            config.collected_writing_rules,
            config.collected_template,
            config.collected_positive_examples,
            config.collected_negative_examples,
        )
    )
    return {
        "schema_version": config.schema_version,
        "config_loaded": config_loaded,
        "shared_writing_rule_count": len(config.shared_writing_rules),
        "personal_boundary_rule_count": len(
            config.personal_event_boundary_rules
        ),
        "personal_template_field_count": len(config.personal_template),
        "personal_positive_example_count": len(
            config.personal_positive_examples
        ),
        "personal_negative_example_count": len(
            config.personal_negative_examples
        ),
        "collected_writing_rule_count": len(config.collected_writing_rules),
        "collected_template_field_count": len(config.collected_template),
        "collected_positive_example_count": len(
            config.collected_positive_examples
        ),
        "collected_negative_example_count": len(
            config.collected_negative_examples
        ),
    }


def event_generation_debug_metadata(
    config: EventGenerationConfig,
    *,
    guidance_mode: str,
    template_mode: str,
    examples_included: bool,
) -> dict[str, object]:
    return {
        "guidance_mode": guidance_mode,
        "template_mode": template_mode,
        "examples_included": examples_included,
        "config": event_generation_debug_summary(config),
    }


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


def load_llm_timeout_seconds(
    config: RuntimeConfig,
    *,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    values = _read_local_env_values(config, cwd=cwd, environ=environ)
    timeout_raw = values.get(config.llm_timeout_env_var, "").strip()
    if not timeout_raw:
        return config.analyzer_timeout_seconds
    try:
        timeout_seconds = int(timeout_raw)
    except ValueError as exc:
        raise ValueError(
            f"Invalid LLM timeout: {config.llm_timeout_env_var} must be an integer."
        ) from exc
    if timeout_seconds <= 0:
        raise ValueError(
            f"Invalid LLM timeout: {config.llm_timeout_env_var} must be positive."
        )
    return timeout_seconds


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

    timeout_seconds = load_llm_timeout_seconds(
        config,
        cwd=cwd,
        environ=environ,
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


def load_codex_llm_settings(
    config: RuntimeConfig,
    *,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> CodexLLMSettings:
    """Load Codex model and relay provider only from the local dotenv file."""
    base_dir = cwd or Path.cwd()
    file_values = load_local_env_file(base_dir / config.llm_env_file_name)
    required_keys = [
        config.codex_model_env_var,
        config.codex_reasoning_effort_env_var,
        config.codex_provider_id_env_var,
        config.codex_provider_name_env_var,
        config.codex_provider_base_url_env_var,
        config.codex_provider_wire_api_env_var,
        config.codex_provider_requires_openai_auth_env_var,
    ]
    missing = [key for key in required_keys if not file_values.get(key, "").strip()]
    if missing:
        raise ValueError(
            "Missing Codex configuration in repository-local "
            f"`{config.llm_env_file_name}`: {', '.join(missing)}. "
            "Codex model, reasoning effort, and relay provider must be explicit and "
            "cannot be inherited from personal Codex settings."
        )
    provider_id = file_values[config.codex_provider_id_env_var].strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", provider_id):
        raise ValueError(
            f"{config.codex_provider_id_env_var} must contain only letters, digits, "
            "underscores, or hyphens."
        )
    return CodexLLMSettings(
        model=file_values[config.codex_model_env_var].strip(),
        reasoning_effort=file_values[config.codex_reasoning_effort_env_var].strip(),
        provider_id=provider_id,
        provider_name=file_values[config.codex_provider_name_env_var].strip(),
        provider_base_url=file_values[config.codex_provider_base_url_env_var].strip(),
        provider_wire_api=file_values[config.codex_provider_wire_api_env_var].strip(),
        provider_requires_openai_auth=_parse_bool_value(
            file_values[config.codex_provider_requires_openai_auth_env_var],
            env_var=config.codex_provider_requires_openai_auth_env_var,
        ),
        timeout_seconds=load_llm_timeout_seconds(
            config,
            cwd=base_dir,
            environ=environ,
        ),
    )


def load_runtime_config_overrides(
    config: RuntimeConfig,
    *,
    cwd: Path | None = None,
) -> RuntimeConfig:
    base_dir = cwd or Path.cwd()
    config = _apply_runtime_env_overrides(config, cwd=base_dir)
    config = _load_model_input_budget_overrides(config, base_dir=base_dir)
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
    config = _load_event_generation_overrides(config, base_dir=base_dir)
    config = _load_collected_merge_overrides(config, base_dir=base_dir)
    return _load_self_delivery_overrides(config, base_dir=base_dir)


def load_model_input_budget_config(config_path: Path) -> ModelInputBudgetConfig:
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ModelInputBudgetConfig()
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid model input budget config: {config_path} is not valid JSON."
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "default_target_tokens",
        "profiles",
    }:
        raise ValueError(
            "Invalid model input budget config: fields do not match the contract."
        )
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError(
            "Invalid model input budget config: schema_version must be 1."
        )
    default_target_tokens = payload["default_target_tokens"]
    if (
        not isinstance(default_target_tokens, int)
        or isinstance(default_target_tokens, bool)
        or default_target_tokens <= 0
    ):
        raise ValueError(
            "Invalid model input budget config: default_target_tokens must be a positive integer."
        )
    raw_profiles = payload["profiles"]
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise ValueError(
            "Invalid model input budget config: profiles must be a non-empty list."
        )

    profiles: list[ModelInputBudgetProfile] = []
    profile_ids: set[str] = set()
    model_pairs: set[tuple[str, str]] = set()
    expected_profile_fields = {
        "profile_id",
        "primary_model",
        "fallback_model",
        "target_tokens",
        "benchmark_dataset_version",
    }
    for raw_profile in raw_profiles:
        if not isinstance(raw_profile, dict) or set(raw_profile) != expected_profile_fields:
            raise ValueError(
                "Invalid model input budget config: profile fields do not match the contract."
            )
        text_values: dict[str, str] = {}
        for key in (
            "profile_id",
            "primary_model",
            "fallback_model",
            "benchmark_dataset_version",
        ):
            value = raw_profile[key]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"Invalid model input budget config: {key} must be non-empty."
                )
            text_values[key] = value.strip()
        for key in ("profile_id", "benchmark_dataset_version"):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", text_values[key]):
                raise ValueError(
                    "Invalid model input budget config: "
                    f"{key} must contain only safe identifier characters."
                )
        target_tokens = raw_profile["target_tokens"]
        if (
            not isinstance(target_tokens, int)
            or isinstance(target_tokens, bool)
            or target_tokens <= 0
        ):
            raise ValueError(
                "Invalid model input budget config: target_tokens must be a positive integer."
            )
        model_pair = (
            text_values["primary_model"],
            text_values["fallback_model"],
        )
        if text_values["profile_id"] in profile_ids:
            raise ValueError(
                "Invalid model input budget config: profile_id values must be unique."
            )
        if model_pair in model_pairs:
            raise ValueError(
                "Invalid model input budget config: model combinations must be unique."
            )
        profile_ids.add(text_values["profile_id"])
        model_pairs.add(model_pair)
        profiles.append(
            ModelInputBudgetProfile(
                profile_id=text_values["profile_id"],
                primary_model=text_values["primary_model"],
                fallback_model=text_values["fallback_model"],
                target_tokens=target_tokens,
                benchmark_dataset_version=text_values[
                    "benchmark_dataset_version"
                ],
            )
        )
    return ModelInputBudgetConfig(
        schema_version=1,
        default_target_tokens=default_target_tokens,
        profiles=tuple(profiles),
    )


def _load_model_input_budget_overrides(
    config: RuntimeConfig,
    *,
    base_dir: Path,
) -> RuntimeConfig:
    budget = load_model_input_budget_config(
        base_dir / config.model_input_budget_file_name
    )
    values = _read_local_env_values(config, cwd=base_dir)
    primary_model = values.get(config.codex_model_env_var, "").strip()
    fallback_model = values.get(config.llm_model_env_var, "").strip()
    profile = next(
        (
            item
            for item in budget.profiles
            if item.primary_model == primary_model
            and item.fallback_model == fallback_model
        ),
        None,
    )
    if profile is None:
        selection = ModelInputBudgetSelection(
            profile_matched=False,
            target_tokens=budget.default_target_tokens,
        )
    else:
        selection = ModelInputBudgetSelection(
            profile_matched=True,
            profile_id=profile.profile_id,
            target_tokens=profile.target_tokens,
            benchmark_dataset_version=profile.benchmark_dataset_version,
        )
    return replace(
        config,
        model_input_budget=budget,
        model_input_budget_selection=selection,
        model_input_batch_target_tokens=selection.target_tokens,
    )


def _load_self_delivery_overrides(
    config: RuntimeConfig,
    *,
    base_dir: Path,
) -> RuntimeConfig:
    config_path = base_dir / config.self_delivery_file_name
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid self delivery config: {config_path} is not valid JSON."
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {"enabled"}:
        raise ValueError(
            "Invalid self delivery config: fields must match the contract."
        )
    if not isinstance(payload["enabled"], bool):
        raise ValueError(
            "Invalid self delivery config: `enabled` must be a boolean."
        )
    return replace(config, self_delivery_enabled=payload["enabled"])


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


def _load_event_generation_overrides(
    config: RuntimeConfig,
    *,
    base_dir: Path,
) -> RuntimeConfig:
    config_path = base_dir / config.event_generation_file_name
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid event generation config: {config_path} is not valid JSON."
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "shared_writing_rules",
        "personal",
        "collected",
    }:
        raise ValueError(
            "Invalid event generation config: fields do not match the contract."
        )
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError(
            "Invalid event generation config: schema_version must be 1."
        )

    personal = payload["personal"]
    collected = payload["collected"]
    if not isinstance(personal, dict) or set(personal) != {
        "event_boundary_rules",
        "template",
        "positive_examples",
        "negative_examples",
    }:
        raise ValueError(
            "Invalid event generation config: personal fields do not match the contract."
        )
    if not isinstance(collected, dict) or set(collected) != {
        "writing_rules",
        "template",
        "positive_examples",
        "negative_examples",
    }:
        raise ValueError(
            "Invalid event generation config: collected fields do not match the contract."
        )

    shared_writing_rules = _read_string_list(
        payload,
        key="shared_writing_rules",
        fallback=(),
        file_path=config_path,
        error_prefix="Invalid event generation config",
    )
    personal_event_boundary_rules = _read_string_list(
        personal,
        key="event_boundary_rules",
        fallback=(),
        file_path=config_path,
        error_prefix="Invalid event generation config",
    )
    collected_writing_rules = _read_string_list(
        collected,
        key="writing_rules",
        fallback=(),
        file_path=config_path,
        error_prefix="Invalid event generation config",
    )
    personal_template = _read_event_generation_template(
        personal["template"],
        expected_fields=(
            "topic",
            "content",
            "action_label",
            "object_hint",
            "retention_detail",
        ),
        config_path=config_path,
        section="personal",
    )
    collected_template = _read_event_generation_template(
        collected["template"],
        expected_fields=(
            "summary_title",
            "summary_content",
            "summary_object_hint",
            "title",
            "content",
            "object_hint",
            "retention_detail",
        ),
        config_path=config_path,
        section="collected",
    )
    personal_positive_examples = _read_event_generation_examples(
        personal["positive_examples"],
        expected_fields=tuple(key for key, _ in personal_template),
        config_path=config_path,
        section="personal.positive_examples",
    )
    personal_negative_examples = _read_event_generation_negative_examples(
        personal["negative_examples"],
        config_path=config_path,
        section="personal.negative_examples",
    )
    collected_positive_examples = _read_event_generation_examples(
        collected["positive_examples"],
        expected_fields=tuple(key for key, _ in collected_template),
        config_path=config_path,
        section="collected.positive_examples",
    )
    collected_negative_examples = _read_event_generation_negative_examples(
        collected["negative_examples"],
        config_path=config_path,
        section="collected.negative_examples",
    )
    for section, positive_examples, negative_examples in (
        ("personal", personal_positive_examples, personal_negative_examples),
        ("collected", collected_positive_examples, collected_negative_examples),
    ):
        names = [item.name for item in (*positive_examples, *negative_examples)]
        if len(names) != len(set(names)):
            raise ValueError(
                "Invalid event generation config: "
                f"{section} example names must be unique."
            )
    if not all(
        (
            shared_writing_rules,
            personal_event_boundary_rules,
            collected_writing_rules,
            personal_positive_examples,
            personal_negative_examples,
            collected_positive_examples,
            collected_negative_examples,
        )
    ):
        raise ValueError(
            "Invalid event generation config: rules and examples must not be empty."
        )
    return replace(
        config,
        event_generation=EventGenerationConfig(
            schema_version=1,
            shared_writing_rules=shared_writing_rules,
            personal_event_boundary_rules=personal_event_boundary_rules,
            personal_template=personal_template,
            personal_positive_examples=personal_positive_examples,
            personal_negative_examples=personal_negative_examples,
            collected_writing_rules=collected_writing_rules,
            collected_template=collected_template,
            collected_positive_examples=collected_positive_examples,
            collected_negative_examples=collected_negative_examples,
        ),
    )


def _read_event_generation_template(
    raw_value: object,
    *,
    expected_fields: tuple[str, ...],
    config_path: Path,
    section: str,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw_value, dict) or set(raw_value) != set(expected_fields):
        raise ValueError(
            "Invalid event generation config: "
            f"{section}.template fields do not match the contract."
        )
    result: list[tuple[str, str]] = []
    for field_name in expected_fields:
        value = raw_value[field_name]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                "Invalid event generation config: "
                f"{config_path} {section}.template.{field_name} must be non-empty."
            )
        result.append((field_name, value.strip()))
    return tuple(result)


def _read_event_generation_examples(
    raw_value: object,
    *,
    expected_fields: tuple[str, ...],
    config_path: Path,
    section: str,
) -> tuple[EventGenerationExample, ...]:
    if not isinstance(raw_value, list) or not raw_value:
        raise ValueError(
            f"Invalid event generation config: {section} must be a non-empty list."
        )
    examples: list[EventGenerationExample] = []
    for item in raw_value:
        if not isinstance(item, dict) or set(item) != {
            "name",
            "source_facts",
            "expected_output",
        }:
            raise ValueError(
                f"Invalid event generation config: {section} fields do not match the contract."
            )
        name = item["name"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"Invalid event generation config: {section} name must be non-empty."
            )
        source_facts = _read_string_list(
            item,
            key="source_facts",
            fallback=(),
            file_path=config_path,
            error_prefix="Invalid event generation config",
        )
        if not source_facts:
            raise ValueError(
                f"Invalid event generation config: {section} source_facts must not be empty."
            )
        expected = _read_event_generation_template(
            item["expected_output"],
            expected_fields=expected_fields,
            config_path=config_path,
            section=f"{section}.{name.strip()}.expected_output",
        )
        examples.append(
            EventGenerationExample(
                name=name.strip(),
                source_facts=source_facts,
                expected_output=expected,
            )
        )
    names = [item.name for item in examples]
    if len(names) != len(set(names)):
        raise ValueError(
            f"Invalid event generation config: {section} names must be unique."
        )
    return tuple(examples)


def _read_event_generation_negative_examples(
    raw_value: object,
    *,
    config_path: Path,
    section: str,
) -> tuple[EventGenerationNegativeExample, ...]:
    if not isinstance(raw_value, list) or not raw_value:
        raise ValueError(
            f"Invalid event generation config: {section} must be a non-empty list."
        )
    examples: list[EventGenerationNegativeExample] = []
    for item in raw_value:
        if not isinstance(item, dict) or set(item) != {
            "name",
            "source_facts",
            "incorrect_behavior",
            "expected_behavior",
        }:
            raise ValueError(
                f"Invalid event generation config: {section} fields do not match the contract."
            )
        name = item["name"]
        incorrect_behavior = item["incorrect_behavior"]
        expected_behavior = item["expected_behavior"]
        if not all(
            isinstance(value, str) and value.strip()
            for value in (name, incorrect_behavior, expected_behavior)
        ):
            raise ValueError(
                f"Invalid event generation config: {section} text must be non-empty."
            )
        source_facts = _read_string_list(
            item,
            key="source_facts",
            fallback=(),
            file_path=config_path,
            error_prefix="Invalid event generation config",
        )
        if not source_facts:
            raise ValueError(
                f"Invalid event generation config: {section} source_facts must not be empty."
            )
        examples.append(
            EventGenerationNegativeExample(
                name=name.strip(),
                source_facts=source_facts,
                incorrect_behavior=incorrect_behavior.strip(),
                expected_behavior=expected_behavior.strip(),
            )
        )
    names = [item.name for item in examples]
    if len(names) != len(set(names)):
        raise ValueError(
            f"Invalid event generation config: {section} names must be unique."
        )
    return tuple(examples)


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
        "attachment_name_normalization",
        "collected_group_discovery_rules",
        "personal_group_discovery_rules",
        "personal_grouping_negative_examples",
        "personal_grouping_positive_examples",
        "personal_grouping_rules",
        "personal_review_rules",
        "collected_review_rules",
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
    collected_review_rules = _read_string_list(
        payload,
        key="collected_review_rules",
        fallback=(),
        file_path=config_path,
        error_prefix="Invalid event grouping config",
    )
    negative_examples = _read_string_list(
        payload,
        key="personal_grouping_negative_examples",
        fallback=(),
        file_path=config_path,
        error_prefix="Invalid event grouping config",
    )
    positive_examples = _read_string_list(
        payload,
        key="personal_grouping_positive_examples",
        fallback=(),
        file_path=config_path,
        error_prefix="Invalid event grouping config",
    )
    discovery_rules = _read_string_list(
        payload,
        key="personal_group_discovery_rules",
        fallback=(),
        file_path=config_path,
        error_prefix="Invalid event grouping config",
    )
    collected_discovery_rules = _read_string_list(
        payload,
        key="collected_group_discovery_rules",
        fallback=(),
        file_path=config_path,
        error_prefix="Invalid event grouping config",
    )
    attachment_normalization = payload["attachment_name_normalization"]
    if not isinstance(attachment_normalization, dict) or set(
        attachment_normalization
    ) != {
        "ignored_extensions",
        "ignored_mime_type_prefixes",
        "version_suffix_patterns",
    }:
        raise ValueError(
            "Invalid event grouping config: attachment_name_normalization fields "
            "do not match the contract."
        )
    ignored_mime_type_prefixes = _read_string_list(
        attachment_normalization,
        key="ignored_mime_type_prefixes",
        fallback=(),
        file_path=config_path,
        error_prefix="Invalid event grouping config",
    )
    ignored_extensions = _read_string_list(
        attachment_normalization,
        key="ignored_extensions",
        fallback=(),
        file_path=config_path,
        error_prefix="Invalid event grouping config",
    )
    version_suffix_patterns = _read_string_list(
        attachment_normalization,
        key="version_suffix_patterns",
        fallback=(),
        file_path=config_path,
        error_prefix="Invalid event grouping config",
    )
    try:
        for pattern in version_suffix_patterns:
            re.compile(pattern)
    except re.error as exc:
        raise ValueError(
            "Invalid event grouping config: version suffix pattern is invalid."
        ) from exc
    if not all(
        (
            grouping_rules,
            negative_examples,
            positive_examples,
            discovery_rules,
            collected_discovery_rules,
            review_rules,
            collected_review_rules,
            ignored_mime_type_prefixes,
            ignored_extensions,
            version_suffix_patterns,
        )
    ):
        raise ValueError(
            "Invalid event grouping config: personal rules, examples and attachment "
            "normalization must not be empty."
        )
    definitions = _read_collected_group_reason_definitions(
        payload["group_reason_definitions"],
        config_path=config_path,
        error_prefix="Invalid event grouping config",
    )
    return replace(
        config,
        personal_grouping_negative_examples=negative_examples,
        personal_grouping_positive_examples=positive_examples,
        personal_group_discovery_rules=discovery_rules,
        collected_group_discovery_rules=collected_discovery_rules,
        personal_grouping_rules=grouping_rules,
        personal_group_review_rules=review_rules,
        collected_group_review_rules=collected_review_rules,
        attachment_ignored_mime_type_prefixes=ignored_mime_type_prefixes,
        attachment_ignored_extensions=ignored_extensions,
        attachment_version_suffix_patterns=version_suffix_patterns,
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
    common_keys = {
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
    retry_keys = {"primary_request_retry_limit", "online_request_retry_limit"}
    present_retry_keys = retry_keys.intersection(payload)
    if len(present_retry_keys) != 1:
        raise ValueError(
            "Invalid LLM retry config: provide exactly one of "
            "primary_request_retry_limit or the compatibility alias "
            "online_request_retry_limit."
        )
    keys = common_keys | present_retry_keys
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
    primary_retry_limit = values[next(iter(present_retry_keys))]
    return replace(
        config,
        primary_request_retry_limit=primary_retry_limit,
        online_request_retry_limit=primary_retry_limit,
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
    supported_keys = {
        "action_labels",
        "self_relations",
        "manual_edit_field_label",
        "manual_edit_types",
    }
    unexpected_keys = sorted(set(payload).difference(supported_keys))
    if unexpected_keys:
        raise ValueError(
            "Invalid event metadata config: unsupported keys "
            f"({', '.join(unexpected_keys)})."
        )

    action_label_types = _read_event_metadata_items(
        payload,
        field_name="action_labels",
        fallback=config.action_label_types,
        metadata_path=metadata_path,
    )
    self_relation_types = _read_event_metadata_items(
        payload,
        field_name="self_relations",
        fallback=config.self_relation_types,
        metadata_path=metadata_path,
    )
    manual_edit_types = _read_event_metadata_items(
        payload,
        field_name="manual_edit_types",
        fallback=config.manual_edit_types,
        metadata_path=metadata_path,
    )
    manual_edit_field_label = payload.get(
        "manual_edit_field_label",
        config.manual_edit_field_label,
    )
    if not isinstance(manual_edit_field_label, str) or not manual_edit_field_label.strip():
        raise ValueError(
            "Invalid event metadata config: `manual_edit_field_label` must be non-empty."
        )

    return replace(
        config,
        action_label_types=action_label_types,
        self_relation_types=self_relation_types,
        manual_edit_types=manual_edit_types,
        manual_edit_field_label=manual_edit_field_label.strip(),
    )


def _read_event_metadata_items(
    payload: dict[str, object],
    *,
    field_name: str,
    fallback: tuple[EventMetadataItem, ...],
    metadata_path: Path,
) -> tuple[EventMetadataItem, ...]:
    raw_items = payload.get(field_name)
    if raw_items is None:
        return fallback
    if not isinstance(raw_items, list):
        raise ValueError(
            f"Invalid event metadata config: {metadata_path} field `{field_name}` must be a list."
        )

    items: list[EventMetadataItem] = []
    seen_keys: set[str] = set()
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise ValueError(
                f"Invalid event metadata config: {metadata_path} `{field_name}` entries must be objects."
            )
        if set(raw_item) != {"key", "label", "order"}:
            raise ValueError(
                f"Invalid event metadata config: {metadata_path} `{field_name}` entries require key, label, and order."
            )
        key = raw_item.get("key")
        label = raw_item.get("label")
        order = raw_item.get("order")
        if not isinstance(key, str) or not key.strip():
            raise ValueError(
                f"Invalid event metadata config: `{field_name}` key must be non-empty."
            )
        if not isinstance(label, str) or not label.strip():
            raise ValueError(
                f"Invalid event metadata config: `{field_name}` label must be non-empty."
            )
        if not isinstance(order, int) or isinstance(order, bool):
            raise ValueError(
                f"Invalid event metadata config: `{field_name}` order must be an integer."
            )
        cleaned_key = key.strip()
        if cleaned_key in seen_keys:
            raise ValueError(
                f"Invalid event metadata config: duplicate `{field_name}` key `{cleaned_key}`."
            )
        seen_keys.add(cleaned_key)
        items.append(
            EventMetadataItem(
                key=cleaned_key,
                label=label.strip(),
                order=order,
            )
        )

    return tuple(sorted(items, key=lambda item: (item.order, item.key)))


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
    analyzer_backend: str = "codex"
    primary_request_retry_limit: int = 1
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
    model_input_batch_target_tokens: int = 7000
    model_input_budget: ModelInputBudgetConfig = field(
        default_factory=ModelInputBudgetConfig
    )
    model_input_budget_selection: ModelInputBudgetSelection = field(
        default_factory=ModelInputBudgetSelection
    )
    collected_merge_missing_field_retry_ratio: float = 0.2
    collected_merge_missing_field_retry_limit: int = 1
    collected_merge_trace_enabled: bool = False
    collected_merge_trace_root: Path = field(
        default_factory=lambda: Path("data") / "debug" / "collected_merge"
    )
    self_delivery_enabled: bool = True
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
    personal_grouping_negative_examples: tuple[str, ...] = ()
    personal_grouping_positive_examples: tuple[str, ...] = ()
    personal_group_discovery_rules: tuple[str, ...] = ()
    collected_group_discovery_rules: tuple[str, ...] = ()
    personal_group_review_rules: tuple[str, ...] = ()
    collected_group_review_rules: tuple[str, ...] = ()
    event_generation: EventGenerationConfig = field(
        default_factory=EventGenerationConfig
    )
    attachment_ignored_mime_type_prefixes: tuple[str, ...] = ()
    attachment_ignored_extensions: tuple[str, ...] = ()
    attachment_version_suffix_patterns: tuple[str, ...] = ()
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
    codex_stdin_mode: bool = True
    anchor_batch_size: int = 3
    sensitive_event_keywords: tuple[str, ...] = ()
    excluded_event_keywords: tuple[str, ...] = ()
    self_assignment_keywords: tuple[str, ...] = ()
    action_label_types: tuple[EventMetadataItem, ...] = ()
    self_relation_types: tuple[EventMetadataItem, ...] = ()
    manual_edit_types: tuple[EventMetadataItem, ...] = DEFAULT_MANUAL_EDIT_TYPES
    manual_edit_field_label: str = "manual_edit"
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
    codex_model_env_var: str = DEFAULT_CODEX_MODEL_ENV_VAR
    codex_reasoning_effort_env_var: str = DEFAULT_CODEX_REASONING_EFFORT_ENV_VAR
    codex_provider_id_env_var: str = DEFAULT_CODEX_PROVIDER_ID_ENV_VAR
    codex_provider_name_env_var: str = DEFAULT_CODEX_PROVIDER_NAME_ENV_VAR
    codex_provider_base_url_env_var: str = DEFAULT_CODEX_PROVIDER_BASE_URL_ENV_VAR
    codex_provider_wire_api_env_var: str = DEFAULT_CODEX_PROVIDER_WIRE_API_ENV_VAR
    codex_provider_requires_openai_auth_env_var: str = (
        DEFAULT_CODEX_PROVIDER_REQUIRES_OPENAI_AUTH_ENV_VAR
    )
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
    event_generation_file_name: str = DEFAULT_EVENT_GENERATION_FILE_NAME
    model_input_budget_file_name: str = DEFAULT_MODEL_INPUT_BUDGET_FILE_NAME
    collected_merge_file_name: str = DEFAULT_COLLECTED_MERGE_FILE_NAME
    retention_policy_file_name: str = DEFAULT_RETENTION_POLICY_FILE_NAME
    self_delivery_file_name: str = DEFAULT_SELF_DELIVERY_FILE_NAME
    llm_stream_enabled: bool = False
    llm_tls_verify: bool = False
    llm_reasoning_effort: str | None = "none"

    def __post_init__(self) -> None:
        primary = self.primary_request_retry_limit
        legacy = self.online_request_retry_limit
        if primary == legacy:
            return
        if primary == 1:
            object.__setattr__(self, "primary_request_retry_limit", legacy)
            return
        if legacy == 1:
            object.__setattr__(self, "online_request_retry_limit", primary)
            return
        raise ValueError(
            "primary_request_retry_limit conflicts with online_request_retry_limit."
        )


DEFAULT_CONFIG = RuntimeConfig()
