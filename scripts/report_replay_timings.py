from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path


CHAT_COMPLETIONS_TIMING_PREFIX = "chat_completions_http.timing "


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--date", default=None, help="Target date in YYYY-MM-DD format.")
    parser.add_argument(
        "--trace-root",
        default=None,
        help="Replay trace directory. Default: data/replay-trace/<date>",
    )
    return parser.parse_args(argv)


def _load_summary(trace_root: Path) -> dict[str, object]:
    summary_path = trace_root / "summary.json"
    return json.loads(summary_path.read_text(encoding="utf-8"))


def _load_llm_usage_summary(
    trace_root: Path,
    target_date: object,
) -> dict[str, object]:
    if not isinstance(target_date, str) or not target_date:
        return {}
    path = trace_root / "conversation_debug" / target_date / "llm_usage.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(payload, dict):
        return {}
    usage = payload.get("usage", {})
    usage_by_kind = usage.get("by_request_kind", {}) if isinstance(usage, dict) else {}
    return {
        "path": str(path.resolve()),
        "exists": True,
        "requests": payload.get("requests", []),
        "by_request_kind": {
            request_kind: {"token_usage": kind_usage}
            for request_kind, kind_usage in usage_by_kind.items()
            if isinstance(request_kind, str) and isinstance(kind_usage, dict)
        }
    }


def _load_stderr_lines(trace_root: Path) -> list[str]:
    stderr_path = trace_root / "run_stderr.log"
    try:
        return stderr_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _load_llm_stderr_lines(trace_root: Path) -> list[str]:
    lines: list[str] = []
    llm_root = trace_root / "llm_calls"
    if not llm_root.exists():
        return lines

    for stderr_path in sorted(llm_root.glob("call_*/stderr.txt")):
        lines.extend(stderr_path.read_text(encoding="utf-8").splitlines())
    return lines


def _to_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _build_basic_stats(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {
            "count": 0,
            "total": 0.0,
            "avg": 0.0,
            "max": 0.0,
            "min": 0.0,
        }
    total = round(sum(values), 3)
    return {
        "count": len(values),
        "total": total,
        "avg": round(total / len(values), 3),
        "max": round(max(values), 3),
        "min": round(min(values), 3),
    }


def _parse_chat_completions_timing_payload(line: str) -> dict[str, object] | None:
    if CHAT_COMPLETIONS_TIMING_PREFIX not in line:
        return None
    payload_text = line.split(CHAT_COMPLETIONS_TIMING_PREFIX, 1)[1].strip()
    if not payload_text:
        return None

    parsed: dict[str, object] = {}
    for token in shlex.split(payload_text):
        if "=" not in token:
            continue
        key, raw_value = token.split("=", 1)
        if not key:
            continue
        try:
            parsed[key] = json.loads(raw_value)
        except json.JSONDecodeError:
            parsed[key] = raw_value
    return parsed or None


def _collect_chat_completions_timings(stderr_lines: list[str]) -> dict[str, object]:
    calls: list[dict[str, object]] = []
    for line in stderr_lines:
        payload = _parse_chat_completions_timing_payload(line)
        if payload is None:
            continue
        payload["time_total_s"] = round(_to_float(payload.get("time_total")), 6)
        payload["time_total_ms"] = round(_to_float(payload.get("time_total")) * 1000, 3)
        calls.append(payload)

    if not calls:
        return {
            "available": False,
            "reason": "No chat_completions_http.timing lines found in llm_calls stderr outputs.",
            "call_count": 0,
            "summary": {},
            "calls": [],
        }

    total_values = [_to_float(item.get("time_total")) for item in calls]
    starttransfer_values = [_to_float(item.get("time_starttransfer")) for item in calls]
    connect_values = [_to_float(item.get("time_connect")) for item in calls]

    return {
        "available": True,
        "reason": "",
        "call_count": len(calls),
        "summary": {
            "time_total_s": _build_basic_stats(total_values),
            "time_starttransfer_s": _build_basic_stats(starttransfer_values),
            "time_connect_s": _build_basic_stats(connect_values),
        },
        "calls": calls,
    }


def _collect_hook_exec_summary(summary: dict[str, object]) -> dict[str, object]:
    llm_summary = summary.get("llm_summary", {})
    calls = llm_summary.get("calls", []) if isinstance(llm_summary, dict) else []
    normalized_calls: list[dict[str, object]] = []
    for index, item in enumerate(calls, start=1):
        if not isinstance(item, dict):
            continue
        normalized_calls.append(
            {
                "call_index": item.get("call_index", index),
                "elapsed_ms": round(_to_float(item.get("elapsed_ms")), 3),
                "returncode": item.get("returncode"),
                "prompt_chars": item.get("prompt_chars"),
                "hook_command": item.get("hook_command"),
            }
        )

    elapsed_values = [_to_float(item.get("elapsed_ms")) for item in normalized_calls]
    return {
        "call_count": len(normalized_calls),
        "summary_ms": _build_basic_stats(elapsed_values),
        "calls": normalized_calls,
    }


def _collect_stage_totals(summary: dict[str, object]) -> list[dict[str, object]]:
    timing_summary = summary.get("timing_summary", {})
    if not isinstance(timing_summary, dict):
        return []
    totals = timing_summary.get("totals_by_event_ms", {})
    run_total = _to_float(totals.get("runner.run.completed"))
    stages: dict[str, dict[str, float | int]] = {}
    events = timing_summary.get("events", [])
    if not isinstance(events, list):
        return []

    for item in events:
        if not isinstance(item, dict) or item.get("event") != "runner.stage.completed":
            continue
        raw_line = str(item.get("raw_line", ""))
        marker = ' stage="'
        if marker not in raw_line:
            continue
        stage = raw_line.split(marker, 1)[1].split('"', 1)[0]
        value = _to_float(item.get("duration_ms"))
        aggregate = stages.setdefault(stage, {"count": 0, "duration_ms": 0.0})
        aggregate["count"] = int(aggregate["count"]) + 1
        aggregate["duration_ms"] = float(aggregate["duration_ms"]) + value

    return [
        {
            "stage": stage,
            "count": aggregate["count"],
            "timing_basis": (
                "wall_clock"
                if stage == "personal_fact_review_all"
                else "parallel_batch_accumulated"
                if stage == "personal_fact_review"
                else "stage_accumulated"
            ),
            "duration_ms": round(float(aggregate["duration_ms"]), 3),
            "duration_s": round(float(aggregate["duration_ms"]) / 1000, 3),
            "share_of_runner_total_pct": round(
                (float(aggregate["duration_ms"]) / run_total * 100) if run_total else 0.0,
                2,
            ),
        }
        for stage, aggregate in sorted(
            stages.items(), key=lambda item: float(item[1]["duration_ms"]), reverse=True
        )
    ]


def _collect_online_llm_summary(summary: dict[str, object]) -> dict[str, object]:
    timing_summary = summary.get("timing_summary", {})
    events = timing_summary.get("events", []) if isinstance(timing_summary, dict) else []
    if not isinstance(events, list):
        events = []
    llm_usage_summary = summary.get("llm_usage_summary", {})
    usage_requests = (
        llm_usage_summary.get("requests", [])
        if isinstance(llm_usage_summary, dict)
        else []
    )
    requests: list[dict[str, object]] = []
    source = "timing_log"
    if isinstance(usage_requests, list) and usage_requests:
        source = "llm_usage.json"
        for item in usage_requests:
            if not isinstance(item, dict):
                continue
            requests.append(
                {
                    "call_index": len(requests) + 1,
                    "request_kind": str(item.get("request_kind", "unknown")),
                    "backend": str(item.get("backend", "online")),
                    "status": str(item.get("status", "success")),
                    "fallback_from": item.get("fallback_from"),
                    "fallback_to": item.get("fallback_to"),
                    "error_category": item.get("error_category"),
                    "duration_ms": round(_to_float(item.get("duration_ms")), 3),
                    "codex_wait_ms": round(_to_float(item.get("codex_wait_ms")), 3),
                    "prompt_chars": item.get("prompt_chars"),
                    "input_tokens": item.get("input_tokens"),
                    "output_tokens": item.get("output_tokens"),
                    "total_tokens": item.get("total_tokens"),
                }
            )
    else:
        for index, item in enumerate(events, start=1):
            if (
                not isinstance(item, dict)
                or item.get("event") != "online_llm.request.completed"
            ):
                continue
            raw_line = str(item.get("raw_line", ""))
            request_kind_match = re.search(r'request_kind="([^"]+)"', raw_line)
            prompt_chars_match = re.search(r'prompt_chars=(\d+)', raw_line)
            requests.append(
                {
                    "call_index": len(requests) + 1,
                    "event_index": index,
                    "request_kind": (
                        request_kind_match.group(1)
                        if request_kind_match
                        else "unknown"
                    ),
                    "backend": "online",
                    "status": "success",
                    "duration_ms": round(_to_float(item.get("duration_ms")), 3),
                    "prompt_chars": (
                        int(prompt_chars_match.group(1))
                        if prompt_chars_match
                        else None
                    ),
                }
            )
    request_durations = [_to_float(item["duration_ms"]) for item in requests]
    codex_wait_durations = [
        _to_float(item.get("codex_wait_ms")) for item in requests
    ]
    requests_by_kind: dict[str, list[dict[str, object]]] = {}
    for item in requests:
        requests_by_kind.setdefault(str(item["request_kind"]), []).append(item)
    usage_by_kind = (
        llm_usage_summary.get("by_request_kind", {})
        if isinstance(llm_usage_summary, dict)
        else {}
    )
    requests_by_backend: dict[str, list[dict[str, object]]] = {}
    for item in requests:
        requests_by_backend.setdefault(str(item.get("backend", "online")), []).append(item)
    return {
        "source": source,
        "request_count": len(requests),
        "request_duration_ms": _build_basic_stats(request_durations),
        "codex_wait_ms": _build_basic_stats(codex_wait_durations),
        "fallback_count": sum(1 for item in requests if item.get("fallback_to")),
        "by_backend": {
            backend: {
                "request_count": len(items),
                "success_count": sum(item.get("status") == "success" for item in items),
                "failed_count": sum(item.get("status") == "failed" for item in items),
                "request_duration_ms": _build_basic_stats(
                    [_to_float(item.get("duration_ms")) for item in items]
                ),
            }
            for backend, items in sorted(requests_by_backend.items())
        },
        "by_request_kind": {
            request_kind: {
                "request_duration_ms": _build_basic_stats(
                    [_to_float(item.get("duration_ms")) for item in items]
                ),
                "token_usage": (
                    usage_by_kind.get(request_kind, {}).get("token_usage", {})
                    if isinstance(usage_by_kind, dict)
                    and isinstance(usage_by_kind.get(request_kind), dict)
                    else {}
                ),
            }
            for request_kind, items in sorted(requests_by_kind.items())
        },
        "calls": requests,
    }


def _collect_personal_fact_review_timing(
    summary: dict[str, object],
) -> dict[str, object]:
    timing_summary = summary.get("timing_summary", {})
    events = timing_summary.get("events", []) if isinstance(timing_summary, dict) else []
    if not isinstance(events, list):
        events = []
    batch_durations: list[float] = []
    wall_clock_durations: list[float] = []
    for item in events:
        if not isinstance(item, dict) or item.get("event") != "runner.stage.completed":
            continue
        raw_line = str(item.get("raw_line", ""))
        stage_match = re.search(r'stage="([^"]+)"', raw_line)
        if stage_match is None:
            continue
        duration_ms = _to_float(item.get("duration_ms"))
        if stage_match.group(1) == "personal_fact_review":
            batch_durations.append(duration_ms)
        elif stage_match.group(1) == "personal_fact_review_all":
            wall_clock_durations.append(duration_ms)

    batch_summary = _build_basic_stats(batch_durations)
    wall_clock_summary = _build_basic_stats(wall_clock_durations)
    wall_clock_total = _to_float(wall_clock_summary.get("total"))
    return {
        "batch_accumulated_ms": batch_summary,
        "wall_clock_ms": wall_clock_summary,
        "accumulated_to_wall_clock_ratio": round(
            _to_float(batch_summary.get("total")) / wall_clock_total,
            3,
        )
        if wall_clock_total
        else None,
    }


def _build_hook_vs_http_summary(
    hook_exec: dict[str, object],
    curl_http: dict[str, object],
) -> dict[str, object]:
    if not curl_http.get("available"):
        return {
            "comparable": False,
            "reason": curl_http.get("reason", "curl metrics unavailable."),
        }

    hook_calls = hook_exec.get("calls", [])
    curl_calls = curl_http.get("calls", [])
    if not isinstance(hook_calls, list) or not isinstance(curl_calls, list):
        return {"comparable": False, "reason": "Invalid call payloads."}
    if len(hook_calls) != len(curl_calls):
        return {
            "comparable": False,
            "reason": f"Call count mismatch: hook={len(hook_calls)} curl={len(curl_calls)}",
        }

    comparisons: list[dict[str, object]] = []
    overhead_values: list[float] = []
    for hook_call, curl_call in zip(hook_calls, curl_calls):
        hook_ms = _to_float(hook_call.get("elapsed_ms"))
        http_ms = _to_float(curl_call.get("time_total_ms"))
        overhead_ms = round(hook_ms - http_ms, 3)
        overhead_values.append(overhead_ms)
        comparisons.append(
            {
                "call_index": hook_call.get("call_index"),
                "hook_exec_ms": round(hook_ms, 3),
                "curl_http_ms": round(http_ms, 3),
                "overhead_ms": overhead_ms,
            }
        )

    return {
        "comparable": True,
        "summary_ms": _build_basic_stats(overhead_values),
        "calls": comparisons,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.trace_root:
        trace_root = Path(args.trace_root)
    elif args.date:
        trace_root = Path("data") / "replay-trace" / args.date
    else:
        raise SystemExit("Either --date or --trace-root is required.")

    summary = _load_summary(trace_root)
    if not isinstance(summary.get("llm_usage_summary"), dict):
        summary["llm_usage_summary"] = _load_llm_usage_summary(
            trace_root,
            summary.get("target_date"),
        )
    stderr_lines = _load_llm_stderr_lines(trace_root)
    if not stderr_lines:
        stderr_lines = _load_stderr_lines(trace_root)
    hook_exec = _collect_hook_exec_summary(summary)
    curl_http = _collect_chat_completions_timings(stderr_lines)

    payload = {
        "target_date": summary.get("target_date"),
        "trace_root": str(trace_root.resolve()),
        "resume_requested": summary.get("resume_requested", False),
        "llm_checkpoint": summary.get("llm_checkpoint_summary", {}),
        "result": summary.get("result"),
        "stage_totals": _collect_stage_totals(summary),
        "online_llm": _collect_online_llm_summary(summary),
        "personal_fact_review_timing": _collect_personal_fact_review_timing(summary),
        "hook_exec": hook_exec,
        "curl_http": curl_http,
        "hook_vs_http": _build_hook_vs_http_summary(hook_exec, curl_http),
    }
    sys_stdout = json.dumps(payload, ensure_ascii=False, indent=2)
    print(sys_stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
