from __future__ import annotations

import json
import ssl
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
from openai import AuthenticationError, PermissionDeniedError, RateLimitError
import httpx

from .config import RuntimeConfig, load_online_llm_settings
from .errors import PreflightError
from .analyzers.online import _extract_text_from_responses_payload


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
) -> CommandResult:
    completed = subprocess.run(
        list(args),
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        input=input_text,
        timeout=timeout,
        check=False,
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
) -> PreflightReport:
    details: dict[str, str] = {}

    try:
        check_python_version(python_version=python_version)
        details["python"] = "ok"

        lark_path = require_command("lark-cli")
        details["lark_cli_path"] = lark_path
        check_lark_identity(command_runner)
        details["lark_identity"] = "ok"

        if config.analyzer_backend == "online":
            ensure_online_runtime_config(config, cwd=cwd)
            details["online_llm_config"] = "ok"
            details.update(probe_online_llm(config, cwd=cwd))
            details["analyzer_backend"] = "online"
        else:
            codex_path = require_command("codex")
            details["codex_path"] = codex_path
            probe_codex(command_runner, cwd=cwd)
            details["analyzer_backend"] = "codex"
            details["codex_probe"] = "ok"

        ensure_data_root_writable(config.data_root)
        details["data_root"] = str(config.data_root.resolve())

        ensure_timezone_available(config.timezone)
        details["timezone"] = config.timezone
    except PreflightError as exc:
        return PreflightReport(ok=False, error_summary=str(exc), details=details)

    return PreflightReport(ok=True, details=details)


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
        raise PreflightError("lark-cli auth status failed.")

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PreflightError("lark-cli auth status did not return valid JSON.") from exc

    identity = payload.get("identity")
    user_info = payload.get("identities", {}).get("user", {})
    available = bool(user_info.get("available"))
    open_id = user_info.get("openId")

    if identity != "user":
        raise PreflightError("lark-cli is not using a user identity.")
    if not available or not open_id:
        raise PreflightError("lark-cli user identity is unavailable or not logged in.")


def probe_codex(command_runner, *, cwd: Path) -> None:
    probe_prompt = 'Return only this compact JSON object: {"probe":"ok"}'
    with tempfile.NamedTemporaryFile(
        prefix="worktrace-codex-probe-",
        suffix=".json",
        dir=str(cwd),
        delete=False,
    ) as handle:
        output_path = Path(handle.name)

    try:
        result = command_runner(
            (
                "codex",
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
                "-s",
                "read-only",
                "-o",
                str(output_path),
                probe_prompt,
            ),
            cwd=cwd,
            timeout=CODEX_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        output_path.unlink(missing_ok=True)
        raise PreflightError("Codex probe timed out.") from exc

    if result.returncode != 0:
        output_path.unlink(missing_ok=True)
        raise PreflightError(classify_codex_failure(result))

    try:
        content = output_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PreflightError("Codex probe did not produce an output file.") from exc
    finally:
        output_path.unlink(missing_ok=True)

    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise PreflightError("Codex probe returned invalid JSON.") from exc

    if not isinstance(payload, dict) or payload.get("probe") != "ok":
        raise PreflightError("Codex probe returned unexpected JSON content.")


def ensure_online_runtime_config(config: RuntimeConfig, *, cwd: Path) -> None:
    try:
        load_online_llm_settings(config, cwd=cwd)
    except ValueError as exc:
        raise PreflightError(str(exc)) from exc


def classify_codex_failure(result: CommandResult) -> str:
    combined = f"{result.stdout}\n{result.stderr}".lower()
    if any(
        token in combined
        for token in (
            "503 service unavailable",
            "service temporarily unavailable",
            "stream disconnected",
            "reconnecting...",
            "unexpected status 503",
        )
    ):
        return "Codex provider or service is temporarily unavailable."
    if "login" in combined or "auth" in combined or "api key" in combined:
        return "Codex is not logged in or lacks permission."
    if any(token in combined for token in ("network", "unreachable", "connection", "timed out")):
        return "Codex network or service is unreachable."
    return "Codex probe failed."


def classify_online_failure(exc: Exception) -> str:
    if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
        return "Online LLM API key is invalid or lacks permission."
    if isinstance(exc, RateLimitError):
        return "Online LLM is rate limited."
    if isinstance(exc, APIStatusError):
        if exc.status_code >= 500:
            return "Online LLM upstream provider or service is temporarily unavailable."
        return f"HTTP {exc.status_code}: {exc.message}"
    if isinstance(exc, APITimeoutError):
        return "Online LLM probe timed out."
    if isinstance(exc, APIConnectionError):
        reason = str(exc)
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
            kwargs: dict[str, object] = {
                "model": settings.model,
                "input": '请严格输出 JSON：{"probe":"ok"}\n/no_think',
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "worktrace_probe",
                        "schema": probe_schema,
                        "strict": True,
                    }
                },
            }
            if settings.reasoning_effort == "none":
                kwargs["reasoning"] = {"effort": "none"}
            response = client.responses.create(**kwargs)
            payload = response.model_dump()
    except Exception as exc:
        raise PreflightError(classify_online_failure(exc)) from exc

    output_text = _extract_text_from_responses_payload(payload)
    if not output_text.strip():
        raise PreflightError("Online LLM probe returned invalid JSON.")

    try:
        normalized = json.loads(output_text)
    except json.JSONDecodeError as exc:
        raise PreflightError("Online LLM probe returned invalid JSON.") from exc
    if normalized.get("probe") != "ok":
        raise PreflightError("Online LLM probe returned unexpected JSON content.")

    return {
        "online_probe": "ok",
        "tls_verify": str(settings.tls_verify).lower(),
        "reasoning_effort": settings.reasoning_effort or "",
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
