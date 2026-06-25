from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from .config import HookLLMSettings, RuntimeConfig, load_hook_llm_settings
from .utils.json_io import dump_json, parse_json_value_from_text


def _run_codex_via_stdin(prompt: str, *, cwd: Path) -> int:
    with tempfile.NamedTemporaryFile(
        prefix="worktrace-hook-",
        suffix=".json",
        dir=str(cwd),
        delete=False,
    ) as handle:
        output_path = Path(handle.name)
    command = [
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
    ]
    schema_path = os.environ.get("WORKTRACE_HOOK_SCHEMA_PATH", "").strip()
    if schema_path:
        command.extend(["--output-schema", schema_path])
    command.append("-")

    result = subprocess.run(
        command,
        cwd=str(cwd),
        input=prompt,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        if result.returncode != 0:
            sys.stderr.write(result.stderr)
            return result.returncode

        try:
            content = output_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            sys.stderr.write(
                "Codex hook command succeeded but did not produce an output file.\n"
            )
            return 1
    finally:
        output_path.unlink(missing_ok=True)
    try:
        normalized = parse_json_value_from_text(content)
    except ValueError:
        sys.stderr.write("Codex hook command produced non-normalizable JSON output.\n")
        return 1
    sys.stdout.write(dump_json(normalized))
    return 0


def _responses_output_to_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "output_text":
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
            elif item_type == "text":
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        return "".join(chunks)
    if isinstance(value, dict):
        for key in ("text", "content", "output_text"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
            if isinstance(candidate, (dict, list)):
                nested = _responses_output_to_text(candidate)
                if nested:
                    return nested
    return ""


def _extract_text_from_responses_payload(payload: object) -> str:
    if isinstance(payload, dict):
        output_text = payload.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text

        output = payload.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                text = _responses_output_to_text(content)
                if text.strip():
                    return text

        for key in ("result", "data", "content", "message"):
            candidate = payload.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate
            if isinstance(candidate, (dict, list)):
                nested = _extract_text_from_responses_payload(candidate)
                if nested.strip():
                    return nested
    if isinstance(payload, str):
        return payload
    return ""


def _build_responses_request_body(
    prompt: str,
    *,
    settings: HookLLMSettings,
    schema_path: str,
) -> dict[str, object]:
    body: dict[str, object] = {
        "model": settings.model,
        "input": prompt,
    }
    if schema_path:
        try:
            schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise RuntimeError(f"Failed to read output schema file: {schema_path}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Output schema file is not valid JSON: {schema_path}") from exc
        body["text"] = {
            "format": {
                "type": "json_schema",
                "name": "worktrace_output",
                "schema": schema,
                "strict": True,
            }
        }
    return body


def _post_responses_request(
    prompt: str,
    *,
    settings: HookLLMSettings,
    schema_path: str,
) -> object:
    body = _build_responses_request_body(prompt, settings=settings, schema_path=schema_path)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    base_url = settings.base_url.rstrip("/")
    request = urllib.request.Request(
        f"{base_url}/responses",
        data=data,
        headers={
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=settings.timeout_seconds) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw)


def _run_responses_http(prompt: str, *, cwd: Path, config: RuntimeConfig | None = None) -> int:
    runtime_config = config or RuntimeConfig()
    try:
        settings = load_hook_llm_settings(runtime_config, cwd=cwd, environ=os.environ)
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    schema_path = os.environ.get("WORKTRACE_HOOK_SCHEMA_PATH", "").strip()
    try:
        payload = _post_responses_request(prompt, settings=settings, schema_path=schema_path)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        status_line = f"HTTP {exc.code}"
        if detail:
            sys.stderr.write(f"{status_line}: {detail}\n")
        else:
            sys.stderr.write(f"{status_line}\n")
        return 1
    except urllib.error.URLError as exc:
        sys.stderr.write(f"Network error: {exc.reason}\n")
        return 1
    except TimeoutError:
        sys.stderr.write("Request timed out.\n")
        return 1
    except RuntimeError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    except json.JSONDecodeError:
        sys.stderr.write("Online LLM returned invalid JSON response envelope.\n")
        return 1

    text = _extract_text_from_responses_payload(payload)
    if not text.strip():
        sys.stderr.write("Online LLM response did not contain text output.\n")
        return 1
    try:
        normalized = parse_json_value_from_text(text)
    except ValueError:
        sys.stderr.write("Online LLM response did not contain valid JSON output.\n")
        return 1

    sys.stdout.write(dump_json(normalized))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.worktrace.hook_runner", add_help=True)
    parser.add_argument("--mode", dest="mode", default="responses-http")
    args = parser.parse_args(argv)

    prompt = sys.stdin.read()
    cwd = Path.cwd()

    if args.mode == "responses-http":
        return _run_responses_http(prompt, cwd=cwd)
    if args.mode == "codex-stdin":
        return _run_codex_via_stdin(prompt, cwd=cwd)

    sys.stderr.write(f"Unsupported hook runner mode: {args.mode}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
