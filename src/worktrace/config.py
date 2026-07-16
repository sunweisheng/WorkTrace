from __future__ import annotations

import os
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping


DEFAULT_LLM_BASE_URL_ENV_VAR = "WORKTRACE_LLM_BASE_URL"
DEFAULT_LLM_MODEL_ENV_VAR = "WORKTRACE_LLM_MODEL"
DEFAULT_LLM_API_KEY_ENV_VAR = "WORKTRACE_LLM_API_KEY"
DEFAULT_LLM_TIMEOUT_ENV_VAR = "WORKTRACE_LLM_TIMEOUT_SECONDS"
DEFAULT_LLM_STREAM_ENV_VAR = "WORKTRACE_LLM_STREAM"
DEFAULT_LLM_TLS_VERIFY_ENV_VAR = "WORKTRACE_LLM_TLS_VERIFY"
DEFAULT_LLM_SLEEP_MIN_ENV_VAR = "WORKTRACE_LLM_SLEEP_MIN_SECONDS"
DEFAULT_LLM_SLEEP_MAX_ENV_VAR = "WORKTRACE_LLM_SLEEP_MAX_SECONDS"
DEFAULT_LLM_REASONING_EFFORT_ENV_VAR = "WORKTRACE_LLM_REASONING_EFFORT"
DEFAULT_COLLECTED_MERGE_TRACE_ENV_VAR = "WORKTRACE_COLLECTED_MERGE_TRACE"
DEFAULT_COLLECTED_MERGE_TRACE_ROOT_ENV_VAR = "WORKTRACE_COLLECTED_MERGE_TRACE_ROOT"
DEFAULT_COLLECTED_MERGE_RETRY_RATIO_ENV_VAR = (
    "WORKTRACE_COLLECTED_MERGE_MISSING_FIELD_RETRY_RATIO"
)
DEFAULT_COLLECTED_MERGE_RETRY_LIMIT_ENV_VAR = (
    "WORKTRACE_COLLECTED_MERGE_MISSING_FIELD_RETRY_LIMIT"
)
DEFAULT_COLLECTED_MERGE_RETRYABLE_ERROR_LIMIT_ENV_VAR = (
    "WORKTRACE_COLLECTED_MERGE_RETRYABLE_ERROR_LIMIT"
)
DEFAULT_COLLECTED_MERGE_RETRY_DELAY_ENV_VAR = (
    "WORKTRACE_COLLECTED_MERGE_RETRY_DELAY_SECONDS"
)
DEFAULT_LLM_ENV_FILE_NAME = ".env"
DEFAULT_EVENT_RULES_FILE_NAME = "config/event_rules.json"
DEFAULT_EVENT_METADATA_FILE_NAME = "config/event_metadata.json"
DEFAULT_REACTION_CATALOGS_ROOT = Path("config") / "reaction_catalogs"
DEFAULT_CONVERSATION_BLACKLIST_FILE_NAME = "config/conversation_blacklist.json"
DEFAULT_CONVERSATION_WINDOW_FILE_NAME = "config/conversation_window.json"
DEFAULT_LLM_RETRY_FILE_NAME = "config/llm_retry.json"


@dataclass(frozen=True)
class OnlineLLMSettings:
    base_url: str
    model: str
    api_key: str
    timeout_seconds: int
    stream_first_response_timeout_seconds: int
    stream_enabled: bool
    tls_verify: bool
    sleep_min_seconds: float
    sleep_max_seconds: float
    reasoning_effort: str | None


@dataclass(frozen=True)
class EventMetadataItem:
    key: str
    label: str
    order: int


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


def _parse_positive_float(raw_value: str, *, env_var: str) -> float:
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

    sleep_min_raw = values.get(config.llm_sleep_min_env_var, "").strip()
    sleep_min_seconds = config.llm_sleep_min_seconds
    if sleep_min_raw:
        sleep_min_seconds = _parse_positive_float(
            sleep_min_raw,
            env_var=config.llm_sleep_min_env_var,
        )

    sleep_max_raw = values.get(config.llm_sleep_max_env_var, "").strip()
    sleep_max_seconds = config.llm_sleep_max_seconds
    if sleep_max_raw:
        sleep_max_seconds = _parse_positive_float(
            sleep_max_raw,
            env_var=config.llm_sleep_max_env_var,
        )

    if sleep_min_seconds > sleep_max_seconds:
        raise ValueError(
            "Invalid online LLM delay range: "
            f"{config.llm_sleep_min_env_var} must be less than or equal to "
            f"{config.llm_sleep_max_env_var}."
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
        sleep_min_seconds=sleep_min_seconds,
        sleep_max_seconds=sleep_max_seconds,
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
        return _load_llm_retry_overrides(
            _load_conversation_window_overrides(
                _load_event_metadata_overrides(config, base_dir=base_dir), base_dir=base_dir
            ),
            base_dir=base_dir,
        )
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
        return _load_llm_retry_overrides(
            _load_conversation_window_overrides(
                _load_event_metadata_overrides(config, base_dir=base_dir), base_dir=base_dir
            ),
            base_dir=base_dir,
        )

    config = replace(
        config,
        sensitive_event_keywords=sensitive_event_keywords,
        excluded_event_keywords=excluded_event_keywords,
        self_assignment_keywords=self_assignment_keywords,
    )
    return _load_llm_retry_overrides(
        _load_conversation_window_overrides(
            _load_event_metadata_overrides(config, base_dir=base_dir), base_dir=base_dir
        ),
        base_dir=base_dir,
    )


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
        "segmentation_retry_limit",
        "event_extraction_retry_limit",
        "stream_first_response_timeout_seconds",
        "max_concurrent_llm_requests",
        "max_concurrent_event_extraction_requests",
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
    values = {}
    for key in keys:
        value = payload[key]
        minimum = 1 if key == "stream_first_response_timeout_seconds" else 0
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ValueError(
                f"Invalid LLM retry config: {retry_path} field `{key}` must be at least {minimum}."
            )
        values[key] = value
    return replace(
        config,
        anchor_retry_limit=values["segmentation_retry_limit"],
        analysis_batch_retry_limit=values["event_extraction_retry_limit"],
        stream_first_response_timeout_seconds=values["stream_first_response_timeout_seconds"],
        max_concurrent_llm_requests=values["max_concurrent_llm_requests"],
        max_concurrent_event_extraction_requests=values[
            "max_concurrent_event_extraction_requests"
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
        retry_ratio = _parse_positive_float(
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

    retryable_error_limit_raw = values.get(
        config.collected_merge_retryable_error_limit_env_var,
        "",
    ).strip()
    if retryable_error_limit_raw:
        updates["collected_merge_retryable_error_limit"] = _parse_non_negative_int(
            retryable_error_limit_raw,
            env_var=config.collected_merge_retryable_error_limit_env_var,
        )

    retry_delay_raw = values.get(
        config.collected_merge_retry_delay_env_var,
        "",
    ).strip()
    if retry_delay_raw:
        updates["collected_merge_retry_delay_seconds"] = _parse_positive_float(
            retry_delay_raw,
            env_var=config.collected_merge_retry_delay_env_var,
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
    anchor_retry_limit: int = 3
    analysis_batch_retry_limit: int = 3
    stream_first_response_timeout_seconds: int = 60
    max_concurrent_llm_requests: int = 1
    max_concurrent_event_extraction_requests: int | None = None
    anchor_batch_retry_limit: int = 1
    conversation_segmentation_failure_threshold: int = 2
    reaction_discovery_page_limit: int = 3
    slice_base_limit: int = 150
    max_model_input_tokens: int = 51200
    collected_merge_missing_field_retry_ratio: float = 0.2
    collected_merge_missing_field_retry_limit: int = 1
    collected_merge_retryable_error_limit: int = 1
    collected_merge_retry_delay_seconds: float = 2.0
    collected_merge_trace_enabled: bool = False
    collected_merge_trace_root: Path = field(
        default_factory=lambda: Path("data") / "debug" / "collected_merge"
    )
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
    llm_sleep_min_env_var: str = DEFAULT_LLM_SLEEP_MIN_ENV_VAR
    llm_sleep_max_env_var: str = DEFAULT_LLM_SLEEP_MAX_ENV_VAR
    llm_reasoning_effort_env_var: str = DEFAULT_LLM_REASONING_EFFORT_ENV_VAR
    collected_merge_trace_env_var: str = DEFAULT_COLLECTED_MERGE_TRACE_ENV_VAR
    collected_merge_trace_root_env_var: str = DEFAULT_COLLECTED_MERGE_TRACE_ROOT_ENV_VAR
    collected_merge_retry_ratio_env_var: str = DEFAULT_COLLECTED_MERGE_RETRY_RATIO_ENV_VAR
    collected_merge_retry_limit_env_var: str = DEFAULT_COLLECTED_MERGE_RETRY_LIMIT_ENV_VAR
    collected_merge_retryable_error_limit_env_var: str = (
        DEFAULT_COLLECTED_MERGE_RETRYABLE_ERROR_LIMIT_ENV_VAR
    )
    collected_merge_retry_delay_env_var: str = DEFAULT_COLLECTED_MERGE_RETRY_DELAY_ENV_VAR
    llm_env_file_name: str = DEFAULT_LLM_ENV_FILE_NAME
    event_rules_file_name: str = DEFAULT_EVENT_RULES_FILE_NAME
    event_metadata_file_name: str = DEFAULT_EVENT_METADATA_FILE_NAME
    conversation_blacklist_file_name: str = DEFAULT_CONVERSATION_BLACKLIST_FILE_NAME
    conversation_window_file_name: str = DEFAULT_CONVERSATION_WINDOW_FILE_NAME
    llm_retry_file_name: str = DEFAULT_LLM_RETRY_FILE_NAME
    llm_stream_enabled: bool = False
    llm_tls_verify: bool = False
    llm_sleep_min_seconds: float = 1.0
    llm_sleep_max_seconds: float = 2.0
    llm_reasoning_effort: str | None = "none"


DEFAULT_CONFIG = RuntimeConfig()
