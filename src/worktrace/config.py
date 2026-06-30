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
DEFAULT_LLM_ENV_FILE_NAME = ".env"
DEFAULT_EVENT_RULES_FILE_NAME = "config/event_rules.json"
DEFAULT_CONVERSATION_BLACKLIST_FILE_NAME = "config/conversation_blacklist.json"


@dataclass(frozen=True)
class OnlineLLMSettings:
    base_url: str
    model: str
    api_key: str
    timeout_seconds: int
    stream_enabled: bool
    tls_verify: bool
    sleep_min_seconds: float
    sleep_max_seconds: float
    reasoning_effort: str | None


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
    rules_path = (cwd or Path.cwd()) / config.event_rules_file_name
    try:
        payload = json.loads(rules_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid event rules config: {rules_path} is not valid JSON."
        ) from exc

    if not isinstance(payload, dict):
        raise ValueError(
            f"Invalid event rules config: {rules_path} must contain a JSON object."
        )

    excluded_event_topics = _read_string_list(
        payload,
        key="excluded_event_topics",
        fallback=config.excluded_event_topics,
        file_path=rules_path,
    )
    excluded_event_content_signatures = _read_string_list(
        payload,
        key="excluded_event_content_signatures",
        fallback=config.excluded_event_content_signatures,
        file_path=rules_path,
    )
    confidential_event_keywords = _read_string_list(
        payload,
        key="confidential_event_keywords",
        fallback=config.confidential_event_keywords,
        file_path=rules_path,
    )
    non_work_sensitive_keywords = _read_string_list(
        payload,
        key="non_work_sensitive_keywords",
        fallback=config.non_work_sensitive_keywords,
        file_path=rules_path,
    )

    if (
        excluded_event_topics == config.excluded_event_topics
        and excluded_event_content_signatures
        == config.excluded_event_content_signatures
        and confidential_event_keywords == config.confidential_event_keywords
        and non_work_sensitive_keywords == config.non_work_sensitive_keywords
    ):
        return config

    return replace(
        config,
        excluded_event_topics=excluded_event_topics,
        excluded_event_content_signatures=excluded_event_content_signatures,
        confidential_event_keywords=confidential_event_keywords,
        non_work_sensitive_keywords=non_work_sensitive_keywords,
    )


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
    slice_base_limit: int = 150
    max_model_input_tokens: int = 100000
    slice_retry_limit: int = 3
    prompt_slice_message_limit: int = 40
    prompt_message_char_limit: int = 300
    prompt_attachment_char_limit: int = 800
    prompt_time_format: str = "%H:%M"
    analyzer_timeout_seconds: int = 180
    codex_stdin_mode: bool = False
    anchor_batch_size: int = 3
    confidential_event_keywords: tuple[str, ...] = ()
    non_work_sensitive_keywords: tuple[str, ...] = ()
    excluded_event_topics: tuple[str, ...] = ()
    excluded_event_content_signatures: tuple[str, ...] = ()
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
    llm_env_file_name: str = DEFAULT_LLM_ENV_FILE_NAME
    event_rules_file_name: str = DEFAULT_EVENT_RULES_FILE_NAME
    conversation_blacklist_file_name: str = DEFAULT_CONVERSATION_BLACKLIST_FILE_NAME
    llm_stream_enabled: bool = False
    llm_tls_verify: bool = False
    llm_sleep_min_seconds: float = 1.0
    llm_sleep_max_seconds: float = 2.0
    llm_reasoning_effort: str | None = "none"


DEFAULT_CONFIG = RuntimeConfig()
