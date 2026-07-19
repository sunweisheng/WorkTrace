from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock, local


def extract_usage(payload: object) -> dict[str, int | None]:
    """Extract provider-reported token usage from a Responses API payload."""
    usage = _find_usage(payload)
    return {
        "input_tokens": _read_token_count(usage, "input_tokens", "prompt_tokens"),
        "output_tokens": _read_token_count(usage, "output_tokens", "completion_tokens"),
        "total_tokens": _read_token_count(usage, "total_tokens"),
    }


def _find_usage(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {}
    usage = payload.get("usage")
    if isinstance(usage, dict):
        return usage
    response = payload.get("response")
    if isinstance(response, dict):
        nested_usage = response.get("usage")
        if isinstance(nested_usage, dict):
            return nested_usage
    return {}


def _read_token_count(usage: dict[str, object], *keys: str) -> int | None:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return value
    return None


@dataclass
class LLMUsageRecorder:
    _records: list[dict[str, int | str | None]] = field(default_factory=list)
    _lock: Lock = field(default_factory=Lock)
    _context: local = field(default_factory=local)

    @contextmanager
    def request_context(self, context_id: str):
        previous = getattr(self._context, "request_context_id", None)
        self._context.request_context_id = context_id
        try:
            yield
        finally:
            self._context.request_context_id = previous

    def record(
        self,
        request_kind: str,
        payload: object,
        *,
        duration_ms: float | None = None,
        prompt_chars: int | None = None,
        backend: str = "online",
        status: str = "success",
        fallback_from: str | None = None,
        fallback_to: str | None = None,
        error_category: str | None = None,
        codex_wait_ms: float | None = None,
    ) -> dict[str, int | None]:
        usage = extract_usage(payload)
        with self._lock:
            self._records.append(
                {
                    "request_kind": request_kind,
                    "request_context_id": getattr(
                        self._context,
                        "request_context_id",
                        None,
                    ),
                    "backend": backend,
                    "status": status,
                    "fallback_from": fallback_from,
                    "fallback_to": fallback_to,
                    "error_category": error_category,
                    "token_usage_status": (
                        "reported"
                        if any(value is not None for value in usage.values())
                        else "unavailable"
                    ),
                    "duration_ms": round(duration_ms, 3) if duration_ms is not None else None,
                    "prompt_chars": prompt_chars,
                    "codex_wait_ms": (
                        round(codex_wait_ms, 3)
                        if codex_wait_ms is not None
                        else None
                    ),
                    **usage,
                }
            )
        return usage

    def summary(self) -> dict[str, object]:
        with self._lock:
            records = list(self._records)

        usage_summary = _summarize_records(
            [
                (
                    str(record["request_kind"]),
                    {
                        "input_tokens": record["input_tokens"],
                        "output_tokens": record["output_tokens"],
                        "total_tokens": record["total_tokens"],
                    },
                )
                for record in records
            ]
        )
        return {
            **usage_summary,
            "by_backend": _summarize_attempts(records, key="backend"),
            "fallback_count": sum(1 for record in records if record.get("fallback_to")),
            "codex_wait_ms": _basic_duration_summary(
                [record.get("codex_wait_ms") for record in records]
            ),
        }

    def records(self) -> list[dict[str, int | str | None]]:
        with self._lock:
            return [dict(record) for record in self._records]


def _summarize_records(
    records: list[tuple[str, dict[str, int | None]]],
) -> dict[str, object]:
    by_request_kind: dict[str, list[dict[str, int | None]]] = defaultdict(list)
    for request_kind, usage in records:
        by_request_kind[request_kind].append(usage)

    return {
        **_summarize_usage(records),
        "by_request_kind": {
            request_kind: _summarize_usage(
                [(request_kind, usage) for usage in usages]
            )
            for request_kind, usages in sorted(by_request_kind.items())
        },
    }


def _summarize_usage(
    records: list[tuple[str, dict[str, int | None]]],
) -> dict[str, int]:
    token_keys = ("input_tokens", "output_tokens", "total_tokens")
    summary = {"request_count": len(records)}
    for key in token_keys:
        values = [usage[key] for _, usage in records if usage[key] is not None]
        summary[key] = sum(value for value in values if value is not None)
        summary[f"reported_{key}_request_count"] = len(values)
        summary[f"missing_{key}_request_count"] = len(records) - len(values)
    return summary


def _basic_duration_summary(values: list[object]) -> dict[str, float | int]:
    durations = [float(value) for value in values if isinstance(value, int | float)]
    return {
        "count": len(durations),
        "total": round(sum(durations), 3),
        "max": round(max(durations), 3) if durations else 0.0,
    }


def _summarize_attempts(
    records: list[dict[str, int | str | None]],
    *,
    key: str,
) -> dict[str, dict[str, object]]:
    grouped: dict[str, list[dict[str, int | str | None]]] = defaultdict(list)
    for record in records:
        grouped[str(record.get(key, "unknown"))].append(record)
    return {
        name: {
            "request_count": len(items),
            "success_count": sum(item.get("status") == "success" for item in items),
            "failed_count": sum(item.get("status") == "failed" for item in items),
            "duration_ms": _basic_duration_summary(
                [item.get("duration_ms") for item in items]
            ),
        }
        for name, items in sorted(grouped.items())
    }
