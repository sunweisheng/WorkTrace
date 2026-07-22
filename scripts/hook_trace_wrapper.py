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


def _write_meta(path: Path, payload: dict[str, object]) -> None:
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _emit_status(message: str) -> None:
    sys.stderr.write(f"{_now_utc_iso()} {message}\n")
    sys.stderr.flush()


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
    meta_path = call_dir / "meta.json"
    running_meta: dict[str, object] = {
        "call_index": call_index,
        "target_date": args.target_date,
        "status": "running",
        "started_at_utc": started_at_utc,
        "completed_at_utc": None,
        "elapsed_ms": None,
        "returncode": None,
        "prompt_chars": len(prompt),
        "stdout_chars": 0,
        "stderr_tail": "",
        "output_error": "",
        "hook_command": args.hook_command,
    }
    _write_meta(meta_path, running_meta)
    _emit_status(
        f'hook_llm.call status="running" call_index={call_index} '
        f'target_date="{args.target_date}"'
    )

    stdout_path = call_dir / "stdout.txt"
    stderr_path = call_dir / "stderr.txt"
    stderr_lines: list[str] = []
    try:
        with stdout_path.open("w+", encoding="utf-8") as stdout_stream:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=stdout_stream,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(Path.cwd()),
                env=env,
            )
            if process.stdin is None or process.stderr is None:
                raise RuntimeError("Hook subprocess pipes were not created.")
            try:
                process.stdin.write(prompt)
                process.stdin.close()
                with stderr_path.open("w", encoding="utf-8") as stderr_stream:
                    for line in process.stderr:
                        stderr_lines.append(line)
                        stderr_stream.write(line)
                        stderr_stream.flush()
                        sys.stderr.write(line)
                        sys.stderr.flush()
                returncode = process.wait()
            except BaseException:
                process.kill()
                process.wait()
                raise
            stdout_stream.flush()
            stdout_stream.seek(0)
            stdout_text = stdout_stream.read()
    except BaseException as exc:
        _write_meta(
            meta_path,
            {
                **running_meta,
                "status": "failed",
                "completed_at_utc": _now_utc_iso(),
                "elapsed_ms": round((perf_counter() - started_at) * 1000, 3),
                "stderr_tail": _stderr_tail("".join(stderr_lines)),
                "output_error": f"{type(exc).__name__}: {exc}",
            },
        )
        _emit_status(
            f'hook_llm.call status="failed" call_index={call_index} '
            f'error_type="{type(exc).__name__}"'
        )
        raise

    elapsed_ms = round((perf_counter() - started_at) * 1000, 3)

    stderr_text = "".join(stderr_lines)

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

    status = "success" if returncode == 0 and parsed_output is not None else "failed"
    meta = {
        **running_meta,
        "status": status,
        "completed_at_utc": _now_utc_iso(),
        "elapsed_ms": elapsed_ms,
        "returncode": returncode,
        "stdout_chars": len(stdout_text),
        "stderr_tail": _stderr_tail(stderr_text),
        "output_error": output_error,
    }
    _write_meta(meta_path, meta)
    _emit_status(
        f'hook_llm.call status="{status}" call_index={call_index} '
        f"returncode={returncode} elapsed_ms={elapsed_ms}"
    )

    sys.stdout.write(stdout_text)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
