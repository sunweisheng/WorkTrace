"""Explicit import of nonsecret personal settings; never used at run time."""
from __future__ import annotations

import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from .analyzers.codex import build_codex_subprocess_env
from .config import load_local_env_file
from .runtime_diagnostics import diagnostic_settings
from .utils.commands import run_text_command


@dataclass(frozen=True)
class CodexImportResult:
    status: str
    values: dict[str, str] = field(default_factory=dict)
    preserved_keys: list[str] = field(default_factory=list)
    missing_keys: list[str] = field(default_factory=list)
    auth_source: str = "unknown"
    auth_status: str = "not_checked"
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(vars(self))


def _env_value(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if not isinstance(value, str) or not value.strip():
        return ""
    if any(char in value for char in ("\n", "\r", "'", '"')):
        raise ValueError("Configuration contains a value unsafe for dotenv.")
    return value.strip()


def _write_missing_values(path: Path, values: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines(keepends=True)
    remaining = dict(values)
    for index, line in enumerate(lines):
        match = re.match(r"\s*(?:export\s+)?([A-Z0-9_]+)\s*=", line)
        if match and match[1] in values:
            value = values[match[1]]
            remaining.pop(match[1], None)
            lines[index] = f"{match[1]}='{value}'\n"
    output = "".join(lines)
    if output and not output.endswith("\n"):
        output += "\n"
    output += "".join(f"{key}='{value}'\n" for key, value in remaining.items())
    path.write_text(output, encoding="utf-8")


def import_codex_config(
    *,
    cwd: Path,
    apply: bool = False,
    import_auth: bool = False,
    profile: str | None = None,
    environ: Mapping[str, str] | None = None,
    command_runner=run_text_command,
) -> CodexImportResult:
    if import_auth and not apply:
        return CodexImportResult("blocked", notes=["Authentication import requires --apply."])
    environment = dict(os.environ if environ is None else environ)
    codex_home = Path(environment.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        with (codex_home / "config.toml").open("rb") as source:
            root = tomllib.load(source)
        selected_profile = profile or root.get("profile")
        selected = dict(root)
        if selected_profile:
            selected.update(root["profiles"][selected_profile])
        provider_id = selected.get("model_provider", "openai")
        provider = dict(diagnostic_settings()["builtin_providers"].get(provider_id, {}))
        provider.update(root.get("model_providers", {}).get(provider_id, {}))
        requires_auth = provider.get("requires_openai_auth", False)
        if not isinstance(requires_auth, bool):
            raise ValueError("requires_openai_auth must be a boolean.")
        auth_source = (
            "provider_bearer_token" if provider.get("experimental_bearer_token")
            else "provider_environment" if provider.get("env_key")
            else "codex_store" if requires_auth else "none"
        )
        raw_values = {
            "MODEL": selected.get("model"),
            "REASONING_EFFORT": selected.get("model_reasoning_effort"),
            "PROVIDER_ID": provider_id,
            "PROVIDER_NAME": provider.get("name"),
            "PROVIDER_BASE_URL": provider.get("base_url"),
            "PROVIDER_WIRE_API": provider.get("wire_api", "responses"),
            "PROVIDER_REQUIRES_OPENAI_AUTH": True if import_auth else requires_auth,
        }
        values = {f"WORKTRACE_CODEX_{key}": _env_value(value)
                  for key, value in raw_values.items()}
        endpoint = urlsplit(values["WORKTRACE_CODEX_PROVIDER_BASE_URL"])
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError("Provider URL may contain credentials; import refused.")
        if values["WORKTRACE_CODEX_PROVIDER_BASE_URL"] and (
            endpoint.scheme not in {"http", "https"} or not endpoint.hostname
        ):
            raise ValueError("Provider URL must be HTTP(S).")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", provider_id):
            raise ValueError("Unsupported provider identifier.")
        path = cwd / ".env"
        existing = load_local_env_file(path)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return CodexImportResult("failed", notes=[
            "Cannot import config.toml. Check CODEX_HOME, profile and provider fields; "
            "URLs must not contain credentials. No configuration was changed."])

    preserved = [key for key in values if existing.get(key, "").strip()]
    pending = {key: value for key, value in values.items()
               if value and key not in preserved}
    effective = {**values, **{key: existing[key] for key in preserved}}
    try:
        existing_endpoint = urlsplit(effective["WORKTRACE_CODEX_PROVIDER_BASE_URL"])
    except ValueError:
        return CodexImportResult("failed", notes=[
            "The existing provider URL is malformed; no configuration was changed."])
    if (existing_endpoint.username or existing_endpoint.password
            or existing_endpoint.query or existing_endpoint.fragment):
        return CodexImportResult("failed", notes=[
            "The existing provider URL may contain credentials; preview is refused."])
    missing = [key for key, value in effective.items() if not value]
    notes = ["Existing nonempty values are preserved. Only personal root/profile "
             "settings are imported; session flags and project overrides are excluded."]
    auth_status = ("file_present_unverified" if (codex_home / "auth.json").exists()
                   else "not_checked")
    if auth_source in {"provider_bearer_token", "provider_environment"}:
        notes.append("Personal provider credentials are excluded at run time. "
                     "--apply --import-auth explicitly converts a bearer token to "
                     "Codex file authentication; requires_openai_auth must then be true.")
    if import_auth:
        incompatible = any(
            effective[key] != values[key] for key in (
                "WORKTRACE_CODEX_PROVIDER_ID", "WORKTRACE_CODEX_PROVIDER_BASE_URL",
                "WORKTRACE_CODEX_PROVIDER_REQUIRES_OPENAI_AUTH",
            )
        )
        token = provider.get("experimental_bearer_token")
        if incompatible or missing or not isinstance(token, str) or not token.strip():
            return CodexImportResult("blocked", effective, preserved, missing,
                auth_source, "not_imported", notes + [
                    "Authentication conversion needs a provider bearer token and "
                    "matching local provider settings with requires_openai_auth=true. "
                    "Review and explicitly edit conflicting fields before retrying."])
        env = build_codex_subprocess_env(environment)
        env["CODEX_HOME"] = str(codex_home.resolve())
        try:
            status = command_runner(["codex", "login", "status"],
                                    env=env, timeout=30)
            logged_out = status.returncode != 0 and any(
                marker in f"{status.stdout}\n{status.stderr}".casefold()
                for marker in diagnostic_settings()["logged_out_markers"]
            )
            if (codex_home / "auth.json").exists() or not logged_out:
                return CodexImportResult("blocked", effective, preserved, missing,
                    auth_source, "existing_or_unverified", notes + [
                        "Existing or unverified Codex authentication is preserved. "
                        "Inspect codex login status before making a separate change."])
            login = command_runner(
                ["codex", "-c", 'cli_auth_credentials_store="file"',
                 "login", "--with-api-key"],
                input_text=token.strip() + "\n", env=env, timeout=30,
            )
            if login.returncode != 0:
                return CodexImportResult("failed", effective, preserved, missing,
                    auth_source, "import_failed", notes + [
                        "Codex authentication import failed; .env was not changed."])
            auth_status = "imported_to_codex_file_store"
        except (OSError, subprocess.SubprocessError):
            return CodexImportResult("failed", effective, preserved, missing,
                auth_source, "import_failed", notes + [
                    "Codex CLI could not import authentication; .env was not changed."])
    if apply:
        if missing:
            return CodexImportResult("blocked", effective, preserved, missing,
                auth_source, auth_status, notes + ["Fill missing settings before applying."])
        try:
            _write_missing_values(path, pending)
        except OSError:
            return CodexImportResult("failed", effective, preserved, missing,
                auth_source, auth_status, notes + ["Cannot write repository-local .env."])
    return CodexImportResult("applied" if apply else "preview", effective,
                             preserved, missing, auth_source, auth_status, notes)
