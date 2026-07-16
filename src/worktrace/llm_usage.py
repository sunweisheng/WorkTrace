from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock


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
    _records: list[tuple[str, dict[str, int | None]]] = field(default_factory=list)
    _lock: Lock = field(default_factory=Lock)

    def record(self, request_kind: str, payload: object) -> dict[str, int | None]:
        usage = extract_usage(payload)
        with self._lock:
            self._records.append((request_kind, usage))
        return usage

    def summary(self) -> dict[str, object]:
        with self._lock:
            records = list(self._records)

        return _summarize_records(records)


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
