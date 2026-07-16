from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path


TIMING_RE = re.compile(
    r"(?P<event>[A-Za-z0-9_.-]+)\s+duration_ms=(?P<duration_ms>\d+(?:\.\d+)?)"
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--date", required=True)
    parser.add_argument(
        "--trace-root",
        default=None,
        help="Default: data/replay-trace/<date>",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Keep previous debug artifacts and LLM checkpoints, then resume matching calls.",
    )
    parser.add_argument(
        "--max-model-input-tokens",
        type=int,
        default=None,
        help="Override the model batch input limit for this replay only.",
    )
    return parser.parse_args(argv)


def _safe_rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def _safe_unlink(path: Path) -> None:
    if path.exists():
        path.unlink()


def _collect_timing(stderr_text: str) -> dict[str, object]:
    events: list[dict[str, object]] = []
    totals: dict[str, float] = {}
    counts: Counter[str] = Counter()

    for line in stderr_text.splitlines():
        match = TIMING_RE.search(line)
        if not match:
            continue
        event = match.group("event")
        duration_ms = float(match.group("duration_ms"))
        events.append(
            {
                "event": event,
                "duration_ms": duration_ms,
                "raw_line": line,
            }
        )
        totals[event] = round(totals.get(event, 0.0) + duration_ms, 3)
        counts[event] += 1

    return {
        "events": events,
        "totals_by_event_ms": totals,
        "counts_by_event": dict(counts),
    }


def _collect_llm_summary(trace_root: Path) -> dict[str, object]:
    llm_root = trace_root / "llm_calls"
    if not llm_root.exists():
        return {
            "call_count": 0,
            "total_elapsed_ms": 0.0,
            "avg_elapsed_ms": 0.0,
            "max_elapsed_ms": 0.0,
            "calls": [],
        }

    calls: list[dict[str, object]] = []
    for meta_path in sorted(llm_root.glob("call_*/meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["path"] = str(meta_path.parent.resolve())
        calls.append(meta)

    elapsed_values = [float(call.get("elapsed_ms", 0.0)) for call in calls]
    total_elapsed_ms = round(sum(elapsed_values), 3)
    avg_elapsed_ms = round(total_elapsed_ms / len(calls), 3) if calls else 0.0
    max_elapsed_ms = round(max(elapsed_values), 3) if calls else 0.0
    return {
        "call_count": len(calls),
        "total_elapsed_ms": total_elapsed_ms,
        "avg_elapsed_ms": avg_elapsed_ms,
        "max_elapsed_ms": max_elapsed_ms,
        "calls": calls,
    }


def _collect_first_pass_summary(conversation_debug_root: Path) -> dict[str, object]:
    if not conversation_debug_root.exists():
        return {
            "request_count": 0,
            "total_elapsed_ms": 0.0,
            "avg_elapsed_ms": 0.0,
            "max_elapsed_ms": 0.0,
            "requests": [],
        }

    requests: list[dict[str, object]] = []
    for meta_path in sorted(conversation_debug_root.glob("*/*/pass_01/meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        request_dir = meta_path.parent
        prompt_path = request_dir / "prompt.txt"
        output_path = request_dir / "output.json"
        requests.append(
            {
                "slice_id": meta.get("slice_id"),
                "conversation_id": meta.get("conversation_id"),
                "conversation_name": meta.get("conversation_name"),
                "elapsed_ms": float(meta.get("elapsed_ms", 0.0)),
                "message_count": meta.get("message_count"),
                "candidate_event_count": meta.get("candidate_event_count"),
                "context_request_count": meta.get("context_request_count"),
                "status": meta.get("status"),
                "path": str(request_dir.resolve()),
                "prompt_path": str(prompt_path.resolve()) if prompt_path.exists() else None,
                "output_path": str(output_path.resolve()) if output_path.exists() else None,
            }
        )

    elapsed_values = [float(item["elapsed_ms"]) for item in requests]
    total_elapsed_ms = round(sum(elapsed_values), 3)
    avg_elapsed_ms = round(total_elapsed_ms / len(requests), 3) if requests else 0.0
    max_elapsed_ms = round(max(elapsed_values), 3) if requests else 0.0
    return {
        "request_count": len(requests),
        "total_elapsed_ms": total_elapsed_ms,
        "avg_elapsed_ms": avg_elapsed_ms,
        "max_elapsed_ms": max_elapsed_ms,
        "requests": requests,
    }


def _collect_review_artifact_summary(
    conversation_debug_root: Path,
    target_date: str,
) -> dict[str, object]:
    date_root = conversation_debug_root / target_date
    artifacts: dict[str, object] = {}
    for review_name in ("retention_review", "personal_fact_review"):
        path = date_root / f"{review_name}.json"
        if not path.exists():
            artifacts[review_name] = {
                "path": str(path.resolve()),
                "exists": False,
                "summary": {},
                "attempt_count": 0,
                "failed_attempt_count": 0,
                "error_summary": "",
            }
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        batches = payload.get("batches", []) if isinstance(payload, dict) else []
        batch_items = [item for item in batches if isinstance(item, dict)]
        artifacts[review_name] = {
            "path": str(path.resolve()),
            "exists": True,
            "summary": (
                payload.get("summary", {}) if isinstance(payload, dict) else {}
            ),
            "attempt_count": len(batch_items),
            "failed_attempt_count": sum(
                1 for item in batch_items if item.get("status") == "failed"
            ),
            "error_summary": (
                str(payload.get("error_summary", ""))
                if isinstance(payload, dict)
                else ""
            ),
        }
    return artifacts


def _collect_checkpoint_summary(checkpoint_root: Path) -> dict[str, object]:
    stages = ("segmentation", "analysis")
    return {
        "path": str(checkpoint_root.resolve()),
        "exists": checkpoint_root.exists(),
        "counts_by_stage": {
            stage: len(list((checkpoint_root / stage).glob("*.json")))
            if (checkpoint_root / stage).exists()
            else 0
            for stage in stages
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.max_model_input_tokens is not None and args.max_model_input_tokens <= 0:
        raise SystemExit("--max-model-input-tokens must be positive.")
    repo_root = Path.cwd()
    trace_root = Path(args.trace_root) if args.trace_root else repo_root / "data" / "replay-trace" / args.date
    conversation_debug_root = trace_root / "conversation_debug"
    counter_path = trace_root / "run_state" / "llm_call_counter.txt"

    trace_root.mkdir(parents=True, exist_ok=True)

    output_dir = repo_root / "data" / args.date[:4] / args.date[5:7]
    old_outputs = sorted(output_dir.glob(f"{args.date}-*.md"))
    old_anchor_cache = repo_root / "data" / "cache" / "anchors" / args.date[:4] / args.date[5:7] / args.date
    old_anchor_debug = repo_root / "data" / "anchor-debug" / args.date
    checkpoint_root = repo_root / "data" / "cache" / "llm" / args.date[:4] / args.date[5:7] / args.date
    old_replay_trace = trace_root

    if not args.resume:
        for old_output in old_outputs:
            _safe_unlink(old_output)
        _safe_rmtree(old_anchor_cache)
        _safe_rmtree(old_anchor_debug)
        _safe_rmtree(checkpoint_root)
        _safe_rmtree(old_replay_trace)

    trace_root.mkdir(parents=True, exist_ok=True)
    conversation_debug_root.mkdir(parents=True, exist_ok=True)
    counter_path.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root)
    env["WORKTRACE_REPLAY_TRACE_ROOT"] = str(trace_root)
    env["WORKTRACE_REPLAY_TARGET_DATE"] = args.date
    cli_args = ["--date", args.date]
    if args.resume:
        cli_args.append("--resume")
    config_overrides = ""
    if args.max_model_input_tokens is not None:
        config_overrides = (
            f"    max_model_input_tokens={args.max_model_input_tokens},\n"
        )
    runner_script = (
        "from dataclasses import replace\n"
        "from pathlib import Path\n"
        "from src.worktrace.cli import main\n"
        "from src.worktrace.config import DEFAULT_CONFIG\n"
        "config = replace(\n"
        "    DEFAULT_CONFIG,\n"
        f"    conversation_debug_root=Path({str(conversation_debug_root)!r}),\n"
        f"{config_overrides}"
        ")\n"
        f"raise SystemExit(main({cli_args!r}, config=config))\n"
    )

    completed = subprocess.run(
        ["python3", "-c", runner_script],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    stdout_path = trace_root / "run_stdout.json"
    stderr_path = trace_root / "run_stderr.log"
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")

    timing_summary = _collect_timing(completed.stderr)
    llm_summary = _collect_llm_summary(trace_root)
    first_pass_summary = _collect_first_pass_summary(conversation_debug_root)
    review_artifact_summary = _collect_review_artifact_summary(
        conversation_debug_root,
        args.date,
    )

    result_payload: object | None = None
    if completed.stdout.strip():
        try:
            result_payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            result_payload = {"raw_stdout": completed.stdout}

    summary = {
        "target_date": args.date,
        "resume_requested": args.resume,
        "max_model_input_tokens": args.max_model_input_tokens,
        "trace_root": str(trace_root.resolve()),
        "returncode": completed.returncode,
        "result": result_payload,
        "first_pass_summary": first_pass_summary,
        "review_artifact_summary": review_artifact_summary,
        "llm_summary": llm_summary,
        "timing_summary": timing_summary,
        "llm_checkpoint_summary": _collect_checkpoint_summary(checkpoint_root),
        "cleared_paths": {
            "output_files": [str(path.resolve()) for path in old_outputs],
            "anchor_cache_dir": str(old_anchor_cache.resolve()),
            "anchor_debug_dir": str(old_anchor_debug.resolve()),
            "llm_checkpoint_dir": str(checkpoint_root.resolve()),
            "trace_root": str(trace_root.resolve()),
        },
    }
    (trace_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    sys.stdout.write("\n")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
