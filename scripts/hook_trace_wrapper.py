from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_counter(counter_path: Path) -> int:
    try:
        return int(counter_path.read_text(encoding="utf-8").strip() or "0")
    except (FileNotFoundError, ValueError):
        return 0


def _next_call_index(counter_path: Path) -> int:
    current = _load_counter(counter_path) + 1
    counter_path.parent.mkdir(parents=True, exist_ok=True)
    counter_path.write_text(str(current), encoding="utf-8")
    return current


def _stderr_tail(stderr: str, limit: int = 20) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return "\n".join(lines[-limit:])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--trace-root", required=True)
    parser.add_argument("--counter-path", required=True)
    parser.add_argument("--target-date", required=True)
    parser.add_argument("--hook-command", required=True)
    args = parser.parse_args(argv)

    trace_root = Path(args.trace_root)
    counter_path = Path(args.counter_path)
    call_index = _next_call_index(counter_path)
    call_dir = trace_root / "llm_calls" / f"call_{call_index:03d}"
    call_dir.mkdir(parents=True, exist_ok=True)

    prompt = sys.stdin.read()
    (call_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

    env = os.environ.copy()
    command = shlex.split(args.hook_command)
    started_at_utc = _now_utc_iso()
    started_at = perf_counter()
    completed = subprocess.run(
        command,
        input=prompt,
        capture_output=True,
        text=True,
        cwd=str(Path.cwd()),
        env=env,
        check=False,
    )
    elapsed_ms = round((perf_counter() - started_at) * 1000, 3)

    stdout_text = completed.stdout or ""
    stderr_text = completed.stderr or ""
    (call_dir / "stdout.txt").write_text(stdout_text, encoding="utf-8")
    if stderr_text:
        (call_dir / "stderr.txt").write_text(stderr_text, encoding="utf-8")

    parsed_output: object | None = None
    output_error = ""
    if stdout_text.strip():
        try:
            parsed_output = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            output_error = f"stdout is not valid JSON: {exc}"

    if parsed_output is not None:
        (call_dir / "output.json").write_text(
            json.dumps(parsed_output, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    meta = {
        "call_index": call_index,
        "target_date": args.target_date,
        "started_at_utc": started_at_utc,
        "elapsed_ms": elapsed_ms,
        "returncode": completed.returncode,
        "prompt_chars": len(prompt),
        "stdout_chars": len(stdout_text),
        "stderr_tail": _stderr_tail(stderr_text),
        "output_error": output_error,
        "hook_command": args.hook_command,
    }
    (call_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    sys.stdout.write(stdout_text)
    sys.stderr.write(stderr_text)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
