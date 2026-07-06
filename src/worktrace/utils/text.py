from __future__ import annotations

import re
from collections import OrderedDict


URL_RE = re.compile(r"https?://[^\s)>\]]+")
_SENTENCE_FINAL_MA_RE = re.compile(r"妈(?P<suffix>[\"'”’」』）)\]】》>\s。！？；!?;….]*)$")


def clean_text(value: str) -> str:
    return "\n".join(
        normalize_sentence_final_ma(line.rstrip())
        for line in value.strip().splitlines()
    ).strip()


def normalize_sentence_final_ma(value: str) -> str:
    return _SENTENCE_FINAL_MA_RE.sub(r"吗\g<suffix>", value)


def sanitize_filename_component(value: str) -> str:
    sanitized = re.sub(r'[\\/:*?"<>|\r\n]+', "_", value.strip())
    sanitized = re.sub(r"\s+", "_", sanitized)
    return sanitized.strip(" ._")


def extract_urls(value: str) -> list[str]:
    return list(OrderedDict.fromkeys(URL_RE.findall(value or "")))


def dedupe_paragraphs(value: str) -> str:
    paragraphs = [clean_text(part) for part in re.split(r"\n\s*\n", value or "")]
    unique = [part for part in OrderedDict.fromkeys(filter(None, paragraphs))]
    return "\n\n".join(unique)


def merge_content_texts(contents: list[str]) -> str:
    normalized = [clean_text(item) for item in contents if clean_text(item)]
    if not normalized:
        return ""

    kept: list[str] = []
    for item in normalized:
        if any(item == existing or item in existing for existing in kept):
            continue
        kept = [existing for existing in kept if existing not in item]
        kept.append(item)

    merged = "\n\n".join(kept)
    return dedupe_paragraphs(merged)


def choose_preferred_text(values: list[str], *, max_length: int = 4000) -> str:
    candidates = [clean_text(value) for value in values if clean_text(value)]
    if not candidates:
        return ""

    ranked = sorted(
        enumerate(candidates),
        key=lambda item: (
            0 if len(item[1]) <= max_length else 1,
            -min(len(item[1]), max_length),
            item[0],
        ),
    )
    return ranked[0][1]
