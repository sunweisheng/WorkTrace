from __future__ import annotations

import json
import ssl
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
from openai import AuthenticationError, PermissionDeniedError, RateLimitError
import httpx

from .config import (
    OnlineLLMSettings,
    RuntimeConfig,
    load_codex_llm_settings,
    load_online_llm_settings,
)
from .errors import PreflightError
from .analyzers.function_calls import function_call_spec
from .analyzers.online import (
    _build_online_function_request_body,
    _create_sdk_request,
    _extract_function_arguments_from_chat_payload,
    _extract_function_arguments_from_responses_payload,
)
from .utils.commands import run_text_command
from .runtime_diagnostics import classify_technical_error


MIN_PYTHON = (3, 11)
CODEX_PROBE_TIMEOUT_SECONDS = 45


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class PreflightReport:
    ok: bool
    error_summary: str = ""
    details: dict[str, str] = field(default_factory=dict)


def run_subprocess(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: int | float | None = None,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    completed = run_text_command(
        args,
        cwd=cwd,
        timeout=timeout,
        input_text=input_text,
        env=env,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def run_preflight_checks(
    config: RuntimeConfig,
    *,
    cwd: Path,
    command_runner=run_subprocess,
    python_version: tuple[int, int, int] | None = None,
    full_backend_checks: bool = False,
) -> PreflightReport:
    if full_backend_checks:
        return _run_full_preflight_checks(
            config, cwd=cwd, command_runner=command_runner,
            python_version=python_version,
        )
    details: dict[str, str] = {}

    try:
        check_python_version(python_version=python_version)
        details["python"] = "ok"

        lark_path = require_command("lark-cli")
        details["lark_cli_path"] = lark_path
        check_lark_identity(command_runner)
        details["lark_identity"] = "ok"

        details["llm_mode"] = config.llm_mode
        if config.llm_mode == "online_only":
            details["codex_probe"] = "disabled"
            details["analyzer_backend"] = "online"
            details["online_fallback"] = "disabled"
            online_settings = ensure_online_runtime_config(config, cwd=cwd)
            ensure_reasoning_disabled(online_settings.reasoning_effort)
            details["online_llm_config"] = "ok"
            details.update(probe_online_llm(config, cwd=cwd))
        else:
            details["codex_path"] = require_command("codex")
            codex_settings = load_codex_llm_settings(config, cwd=cwd)
            details["codex_config"] = "ok"
            details["codex_model"] = codex_settings.model
            details["codex_reasoning_effort"] = codex_settings.reasoning_effort
            probe_codex(command_runner, config=config, cwd=cwd)
            details["codex_probe"] = "ok"
            details["analyzer_backend"] = "codex"

            try:
                online_settings = ensure_online_runtime_config(config, cwd=cwd)
                ensure_reasoning_disabled(online_settings.reasoning_effort)
            except PreflightError as exc:
                details["online_fallback"] = "disabled"
                details["online_fallback_warning"] = str(exc)
            else:
                details["online_llm_config"] = "ok"
                details["online_fallback"] = "available"
                details["online_probe_status"] = "not_run"
                details["tls_verify"] = str(online_settings.tls_verify).lower()
                details["certificate_verification"] = (
                    "enabled" if online_settings.tls_verify else "disabled"
                )
                details["online_reasoning_effort"] = online_settings.reasoning_effort or ""
        ensure_data_root_writable(config.data_root)
        details["data_root"] = str(config.data_root.resolve())

        ensure_timezone_available(config.timezone)
        details["timezone"] = config.timezone
    except (PreflightError, ValueError) as exc:
        return PreflightReport(ok=False, error_summary=str(exc), details=details)

    return PreflightReport(ok=True, details=details)


def _run_full_preflight_checks(
    config: RuntimeConfig, *, cwd: Path, command_runner, python_version,
) -> PreflightReport:
    """Probe backends independently without reading any work conversations."""
    details = {"llm_mode": config.llm_mode, "preflight_scope": "full_backends"}
    errors: list[str] = []

    def check(stage, action):
        try:
            action()
            details[stage] = "ok"
        except (PreflightError, ValueError, OSError, subprocess.SubprocessError) as exc:
            details[stage] = "failed"
            if isinstance(exc, PreflightError) and exc.code:
                code, summary = exc.code, str(exc)
            else:
                code, summary = classify_technical_error(str(exc))
            details[f"{stage}_error_code"] = code
            errors.append(f"{stage}: {summary}")

    check("python", lambda: check_python_version(python_version=python_version))

    def lark_check():
        details["lark_cli_path"] = require_command("lark-cli")
        check_lark_identity(command_runner)

    check("lark_identity", lark_check)

    if config.llm_mode != "online_only":
        def codex_check():
            details["codex_path"] = require_command("codex")
            settings = load_codex_llm_settings(config, cwd=cwd)
            details["codex_model"] = settings.model
            details["codex_reasoning_effort"] = settings.reasoning_effort
            probe_codex(command_runner, config=config, cwd=cwd)

        check("codex_probe", codex_check)
    else:
        details["codex_probe"] = "disabled"

    def online_check():
        settings = ensure_online_runtime_config(config, cwd=cwd)
        ensure_reasoning_disabled(settings.reasoning_effort)
        details["tls_verify"] = str(settings.tls_verify).lower()
        details["certificate_verification"] = (
            "enabled" if settings.tls_verify else "disabled"
        )
        details.update(probe_online_llm(config, cwd=cwd))

    check("online_probe", online_check)
    details["online_fallback"] = (
        "disabled" if config.llm_mode == "online_only" else
        "verified" if details["online_probe"] == "ok" else "unverified"
    )
    check("data_root", lambda: ensure_data_root_writable(config.data_root))
    check("timezone", lambda: ensure_timezone_available(config.timezone))
    return PreflightReport(ok=not errors, error_summary="; ".join(errors), details=details)


def check_python_version(
    *,
    python_version: tuple[int, int, int] | None,
) -> None:
    version = python_version or __import__("sys").version_info[:3]
    if version < MIN_PYTHON:
        raise PreflightError(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required, got "
            f"{version[0]}.{version[1]}.{version[2]}."
        )


def require_command(command_name: str) -> str:
    path = shutil.which(command_name)
    if not path:
        raise PreflightError(f"Required command not found: {command_name}.")
    return path


def check_lark_identity(command_runner) -> None:
    result = command_runner(("lark-cli", "auth", "status"))
    if result.returncode != 0:
        code, summary = classify_technical_error(f"{result.stdout}\n{result.stderr}")
        raise PreflightError(summary, code=code)

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PreflightError("lark-cli auth status did not return valid JSON.") from exc

    if not isinstance(payload, dict):
        raise PreflightError("lark-cli auth status did not return a JSON object.")
    if payload.get("error"):
        code, summary = classify_technical_error(json.dumps(payload["error"]))
        raise PreflightError(summary, code=code)
    identity = payload.get("identity")
    identities = payload.get("identities")
    user_info = identities.get("user", {}) if isinstance(identities, dict) else {}
    if not isinstance(user_info, dict):
        user_info = {}
    available = bool(user_info.get("available"))
    open_id = user_info.get("openId")

    if identity != "user":
        raise PreflightError("lark-cli is not using a user identity.")
    if not available or not open_id:
        raise PreflightError("lark-cli user identity is unavailable or not logged in.")


def probe_codex(command_runner, *, config: RuntimeConfig, cwd: Path) -> None:
    from .analyzers.codex import CodexAnalyzer

    probe_schema = {
        "type": "object",
        "properties": {"probe": {"type": "string", "enum": ["ok"]}},
        "required": ["probe"],
        "additionalProperties": False,
    }
    spec = function_call_spec(
        "preflight",
        probe_schema,
        typical_arguments={"probe": "ok"},
    )
    try:
        payload = CodexAnalyzer(
            config=config,
            command_runner=command_runner,
            cwd=cwd,
        ).request_function(
            '请把 probe 设为 "ok" 并提交。',
            function_spec=spec,
        )
    except subprocess.TimeoutExpired as exc:
        raise PreflightError("Codex probe timed out.") from exc
    except ValueError as exc:
        raise PreflightError(str(exc)) from exc
    except Exception as exc:
        code, summary = classify_technical_error(str(exc))
        raise PreflightError(summary, code=code) from exc

    if not isinstance(payload, dict) or payload.get("probe") != "ok":
        raise PreflightError("Codex probe returned unexpected JSON content.")


def ensure_online_runtime_config(config: RuntimeConfig, *, cwd: Path) -> OnlineLLMSettings:
    try:
        return load_online_llm_settings(config, cwd=cwd)
    except ValueError as exc:
        raise PreflightError(str(exc)) from exc


def ensure_reasoning_disabled(reasoning_effort: str | None) -> None:
    if reasoning_effort != "none":
        raise PreflightError(
            "WorkTrace requires WORKTRACE_LLM_REASONING_EFFORT=none in the main flow."
        )


def classify_codex_failure(result: CommandResult) -> str:
    _, summary = classify_technical_error(f"{result.stdout}\n{result.stderr}")
    return summary


def classify_online_failure(exc: Exception) -> str:
    if isinstance(exc, AuthenticationError):
        return classify_technical_error("401")[1]
    if isinstance(exc, PermissionDeniedError):
        return classify_technical_error("403")[1]
    if isinstance(exc, RateLimitError):
        return "Online LLM is rate limited."
    if isinstance(exc, APIStatusError):
        if exc.status_code >= 500:
            return "Online LLM upstream provider or service is temporarily unavailable."
        code, summary = classify_technical_error(str(exc.message))
        return f"HTTP {exc.status_code}: {summary}"
    if isinstance(exc, APITimeoutError):
        return "Online LLM probe timed out."
    if isinstance(exc, APIConnectionError):
        causes: list[str] = []
        seen: set[int] = set()
        cause: BaseException | None = exc
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            causes.append(str(cause))
            cause = cause.__cause__ or cause.__context__
        reason = "\n".join(causes)
        lowered = reason.lower()
        if "certificate verify failed" in lowered:
            return "Online LLM TLS certificate verification failed."
        if "tls" in lowered or "ssl" in lowered:
            return "Online LLM TLS handshake failed."
        return "Online LLM network or service is unreachable."
    return "Online LLM probe failed."


def probe_online_llm(config: RuntimeConfig, *, cwd: Path) -> dict[str, str]:
    settings = load_online_llm_settings(config, cwd=cwd)
    ssl_context = ssl.create_default_context()
    if not settings.tls_verify:
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    try:
        with httpx.Client(
            verify=ssl_context if settings.tls_verify else False,
            timeout=httpx.Timeout(
                min(settings.timeout_seconds, CODEX_PROBE_TIMEOUT_SECONDS),
                connect=min(5.0, settings.timeout_seconds),
            ),
        ) as http_client:
            client = OpenAI(
                api_key=settings.api_key,
                base_url=settings.base_url.strip(),
                http_client=http_client,
                max_retries=0,
            )
            probe_schema = {
                "type": "object",
                "properties": {"probe": {"type": "string"}},
                "required": ["probe"],
                "additionalProperties": False,
            }
            function_spec = function_call_spec(
                "preflight",
                probe_schema,
                typical_arguments={"probe": "ok"},
            )
            request_settings = replace(settings, stream_enabled=False)
            kwargs = _build_online_function_request_body(
                '请只调用指定 Function，并把 probe 设为 "ok"。',
                settings=request_settings,
                function_spec=function_spec,
            )
            try:
                response = _create_sdk_request(
                    client,
                    kwargs,
                    wire_api=settings.wire_api,
                )
                payload = response.model_dump()
            finally:
                close_client = getattr(client, "close", None)
                if callable(close_client):
                    close_client()
    except Exception as exc:
        summary = classify_online_failure(exc)
        code, _ = classify_technical_error(
            f"{getattr(exc, 'status_code', '')} {exc} {summary}"
        )
        raise PreflightError(summary, code=code) from exc

    try:
        if settings.wire_api == "chat_completions":
            normalized = _extract_function_arguments_from_chat_payload(
                payload,
                expected_name=function_spec.name,
            )
        else:
            normalized = _extract_function_arguments_from_responses_payload(
                payload,
                expected_name=function_spec.name,
            )
    except Exception as exc:
        raise PreflightError(
            "Online LLM does not support the required Function Calling contract."
        ) from exc
    if not isinstance(normalized, dict) or normalized.get("probe") != "ok":
        raise PreflightError("Online LLM probe returned unexpected JSON content.")

    return {
        "online_probe": "ok",
        "tls_verify": str(settings.tls_verify).lower(),
        "reasoning_effort": settings.reasoning_effort or "",
        "wire_api": settings.wire_api,
        "certificate_verification": (
            "enabled" if settings.tls_verify else "disabled"
        ),
    }


def ensure_data_root_writable(data_root: Path) -> None:
    try:
        data_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=str(data_root), delete=True):
            pass
    except OSError as exc:
        raise PreflightError(f"Data directory is not writable: {data_root}.") from exc


def ensure_timezone_available(timezone_name: str) -> None:
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise PreflightError(f"Timezone is unavailable: {timezone_name}.") from exc
