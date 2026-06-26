from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import shlex

from .config import RuntimeConfig, load_hook_llm_settings
from .errors import PreflightError


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

        if config.analyzer_backend == "hook":
            hook_args = shlex.split(config.hook_command)
            if not hook_args:
                raise PreflightError("Hook analyzer requires a non-empty hook_command.")
            details["hook_command_path"] = require_executable(hook_args[0])
            ensure_hook_runtime_config(config, cwd=cwd)
            details["hook_llm_config"] = "ok"
            probe_hook(config, command_runner, cwd=cwd)
            details["analyzer_backend"] = "hook"
            details["hook_probe"] = "ok"
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


def require_executable(command_name: str) -> str:
    command_path = Path(command_name).expanduser()
    if "/" in command_name:
        if not command_path.exists():
            raise PreflightError(f"Required command not found: {command_name}.")
        return str(command_path)
    return require_command(command_name)


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


def probe_hook(
    config: RuntimeConfig,
    command_runner,
    *,
    cwd: Path,
) -> None:
    if not config.hook_command.strip():
        raise PreflightError("Hook analyzer requires a non-empty hook_command.")

    probe_prompt = 'Return only this compact JSON object: {"probe":"ok"}'
    try:
        result = command_runner(
            tuple(shlex.split(config.hook_command)),
            cwd=cwd,
            timeout=CODEX_PROBE_TIMEOUT_SECONDS,
            input_text=probe_prompt,
        )
    except subprocess.TimeoutExpired as exc:
        raise PreflightError("Hook probe timed out.") from exc

    if result.returncode != 0:
        raise PreflightError(classify_hook_failure(result))

    try:
        payload = json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        raise PreflightError("Hook probe returned invalid JSON.") from exc

    if not isinstance(payload, dict) or payload.get("probe") != "ok":
        raise PreflightError("Hook probe returned unexpected JSON content.")


def ensure_hook_runtime_config(config: RuntimeConfig, *, cwd: Path) -> None:
    try:
        load_hook_llm_settings(config, cwd=cwd)
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


def classify_hook_failure(result: CommandResult) -> str:
    combined = f"{result.stdout}\n{result.stderr}".lower()
    if "hook analysis command failed" in combined:
        return "Hook analyzer command failed."
    if "missing online llm configuration" in combined:
        return (
            "Online LLM configuration is missing. WorkTrace requires the user to provide local .env "
            "or environment variables before running."
        )
    if "401" in combined or "403" in combined or "unauthorized" in combined or "forbidden" in combined:
        return "Hook analyzer online LLM API key is invalid or lacks permission."
    if "429" in combined or "rate limit" in combined or "too many requests" in combined:
        return "Hook analyzer online LLM is rate limited."
    if any(
        token in combined
        for token in (
            "503 service unavailable",
            "service temporarily unavailable",
            "stream disconnected",
            "reconnecting...",
            "unexpected status 503",
            "500",
            "502",
            "504",
        )
    ):
        return "Hook analyzer upstream provider or service is temporarily unavailable."
    if any(
        token in combined for token in ("network", "unreachable", "connection", "timed out", "request timed out")
    ):
        return "Hook analyzer network or service is unreachable."
    return "Hook probe failed."


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
