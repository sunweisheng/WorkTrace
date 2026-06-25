from __future__ import annotations

import json
from typing import Any


def load_json_object(text: str) -> dict[str, Any]:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object.")
    return data


def dump_json(data: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def parse_json_value_from_text(text: str) -> object:
    stripped = text.strip()
    if not stripped:
        raise ValueError("Expected non-empty JSON text.")

    try:
        return unwrap_common_json_envelope(json.loads(stripped))
    except json.JSONDecodeError:
        pass

    for extractor in (try_extract_json_object_fragment, try_extract_json_array_fragment):
        fragment = extractor(stripped)
        if fragment is None:
            continue
        try:
            return unwrap_common_json_envelope(json.loads(fragment))
        except json.JSONDecodeError:
            continue

    raise ValueError("Could not parse JSON value from text.")


def unwrap_common_json_envelope(value: object) -> object:
    current = value
    while True:
        if isinstance(current, list):
            return current
        if not isinstance(current, dict):
            return current

        unwrapped = False
        for key in ("structured_output", "result", "content", "message", "data"):
            if key not in current:
                continue
            candidate = current[key]
            if isinstance(candidate, str):
                candidate_text = candidate.strip()
                if not candidate_text:
                    continue
                try:
                    current = json.loads(candidate_text)
                    unwrapped = True
                    break
                except json.JSONDecodeError:
                    continue
            if isinstance(candidate, (dict, list)):
                current = candidate
                unwrapped = True
                break
        if not unwrapped:
            return current


def try_extract_json_object_fragment(text: str) -> str | None:
    return _try_extract_json_fragment(text, "{", "}")


def try_extract_json_array_fragment(text: str) -> str | None:
    return _try_extract_json_fragment(text, "[", "]")


def _try_extract_json_fragment(text: str, open_char: str, close_char: str) -> str | None:
    start = text.find(open_char)
    end = text.rfind(close_char)
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]
