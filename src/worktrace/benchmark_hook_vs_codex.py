from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path


def _run_command(args: list[str], *, cwd: Path) -> dict[str, object]:
    started_at = time.perf_counter()
    completed = subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    duration_ms = (time.perf_counter() - started_at) * 1000
    payload: dict[str, object] = {
        "args": args,
        "returncode": completed.returncode,
        "duration_ms": round(duration_ms, 1),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    try:
        payload["json"] = json.loads(completed.stdout) if completed.stdout.strip() else None
    except json.JSONDecodeError:
        payload["json"] = None
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.worktrace.benchmark_hook_vs_codex",
        add_help=True,
    )
    parser.add_argument("--date", dest="target_date", required=True)
    parser.add_argument("--limit", dest="limit", type=int, default=1)
    parser.add_argument("--dump-dir", dest="dump_dir", required=False)
    parser.add_argument("--ignore-cache", dest="ignore_cache", action="store_true")
    args = parser.parse_args(argv)

    cwd = Path.cwd()
    common_args = [
        "python3",
        "-c",
        (
            "from src.worktrace.anchor_experiment import _main_impl; "
            "from src.worktrace.config import RuntimeConfig; "
            "import sys; "
            "cfg = RuntimeConfig(analyzer_backend='codex'); "
            "raise SystemExit(_main_impl("
            "sys.argv[1:], sys.stdout.write, config=cfg, "
            "preflight_func=lambda config, cwd: type('Report', (), {'ok': True, 'error_summary': '', 'details': {}})()"
            "))"
        ),
        "--date",
        args.target_date,
        "--limit",
        str(args.limit),
        "--summary-only",
    ]
    if args.dump_dir:
        common_args.extend(["--dump-dir", args.dump_dir])
    if args.ignore_cache:
        common_args.append("--ignore-cache")

    codex_args = list(common_args)
    hook_args = [
        "python3",
        "-c",
        (
            "from src.worktrace.anchor_experiment import _main_impl; "
            "from src.worktrace.config import RuntimeConfig; "
            "import sys; "
            "cfg = RuntimeConfig(analyzer_backend='hook', "
            "hook_command='python3 -m src.worktrace.hook_runner --mode codex-stdin'); "
            "raise SystemExit(_main_impl("
            "sys.argv[1:], sys.stdout.write, config=cfg, "
            "preflight_func=lambda config, cwd: type('Report', (), {'ok': True, 'error_summary': '', 'details': {}})()"
            "))"
        ),
        "--date",
        args.target_date,
        "--limit",
        str(args.limit),
        "--summary-only",
    ]
    if args.dump_dir:
        hook_args.extend(["--dump-dir", args.dump_dir])
    if args.ignore_cache:
        hook_args.append("--ignore-cache")

    result = {
        "target_date": args.target_date,
        "limit": args.limit,
        "ignore_cache": args.ignore_cache,
        "codex": _run_command(codex_args, cwd=cwd),
        "hook_codex_stdin": _run_command(hook_args, cwd=cwd),
    }
    sys_stdout = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(sys_stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
