from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from .config import RuntimeConfig
from .utils.json_io import dump_json, parse_json_value_from_text

DEFAULT_REPLY_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "reply": {
            "type": "string",
        }
    },
    "required": ["reply"],
    "additionalProperties": False,
}

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _default_runner(
    args: Sequence[str],
    *,
    cwd: Path,
    timeout: int | float,
    input_text: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        input=input_text,
        timeout=timeout,
        env=env,
        check=False,
    )


def _build_prompt(user_text: str) -> str:
    return (
        f"你是一个简洁的中文助手。用户对你说：{user_text}。"
        "请根据提供的输出 schema 返回 JSON，"
        "只在 reply 字段中写你想回复给用户的话。"
    )


def _stderr_tail(stderr: str, *, limit: int = 10) -> str:
    lines = [line for line in stderr.strip().splitlines() if line.strip()]
    return "\n".join(lines[-limit:])


def _run_once(
    *,
    run_index: int,
    hook_command: str,
    prompt: str,
    output_schema: dict[str, object],
    cwd: Path,
    timeout_seconds: int,
    command_runner: CommandRunner = _default_runner,
) -> dict[str, object]:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="worktrace-hook-schema-",
        suffix=".json",
        dir=str(cwd),
        delete=False,
    ) as handle:
        schema_path = Path(handle.name)
        json.dump(output_schema, handle, ensure_ascii=False)

    env = os.environ.copy()
    env["WORKTRACE_HOOK_SCHEMA_PATH"] = str(schema_path)
    started_at = time.perf_counter()
    try:
        completed = command_runner(
            shlex.split(hook_command),
            cwd=cwd,
            timeout=timeout_seconds,
            input_text=prompt,
            env=env,
        )
    finally:
        schema_path.unlink(missing_ok=True)
    elapsed_seconds = round(time.perf_counter() - started_at, 3)

    result: dict[str, object] = {
        "run": run_index,
        "elapsed_seconds": elapsed_seconds,
        "returncode": completed.returncode,
        "stdout_raw": completed.stdout.strip(),
        "stderr_tail": _stderr_tail(completed.stderr),
    }
    if completed.returncode == 0 and completed.stdout.strip():
        try:
            result["parsed"] = parse_json_value_from_text(completed.stdout)
        except ValueError as exc:
            result["parse_error"] = str(exc)
    return result


def _build_stats(runs: list[dict[str, object]]) -> dict[str, object]:
    if not runs:
        return {
            "success_count": 0,
            "failure_count": 0,
            "avg_elapsed_seconds": 0.0,
            "min_elapsed_seconds": 0.0,
            "max_elapsed_seconds": 0.0,
        }

    durations = [float(run["elapsed_seconds"]) for run in runs]
    success_count = sum(1 for run in runs if run["returncode"] == 0)
    return {
        "success_count": success_count,
        "failure_count": len(runs) - success_count,
        "avg_elapsed_seconds": round(sum(durations) / len(durations), 3),
        "min_elapsed_seconds": round(min(durations), 3),
        "max_elapsed_seconds": round(max(durations), 3),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.worktrace.benchmark_hook_hello",
        add_help=True,
    )
    parser.add_argument("--prompt", dest="prompt", default="你好")
    parser.add_argument("--runs", dest="runs", type=int, default=5)
    parser.add_argument(
        "--hook-command",
        dest="hook_command",
        default=RuntimeConfig().hook_command,
    )
    parser.add_argument("--timeout-seconds", dest="timeout_seconds", type=int, default=240)
    args = parser.parse_args(argv)

    if args.runs <= 0:
        raise SystemExit("--runs must be a positive integer.")

    cwd = Path.cwd()
    prompt = _build_prompt(args.prompt)
    runs = [
        _run_once(
            run_index=index,
            hook_command=args.hook_command,
            prompt=prompt,
            output_schema=DEFAULT_REPLY_SCHEMA,
            cwd=cwd,
            timeout_seconds=args.timeout_seconds,
        )
        for index in range(1, args.runs + 1)
    ]
    payload = {
        "prompt": args.prompt,
        "hook_command": args.hook_command,
        "schema": DEFAULT_REPLY_SCHEMA,
        "runs": runs,
        "stats": _build_stats(runs),
    }
    print(dump_json(payload, pretty=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
