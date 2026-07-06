from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .text import sanitize_filename_component


_DATE_RE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
_MERGED_TOKEN_RE = re.compile(r"(?i)(?:^|[-_\s])merged(?:$|[-_\s])")
_EDGE_SEPARATOR_RE = re.compile(r"^[-_\s]+|[-_\s]+$")


@dataclass(frozen=True)
class ParsedWorktraceFilename:
    filename: str
    stem: str
    suffix: str
    target_date: str
    owner_name: str
    is_merged: bool


def build_personal_markdown_filename(target_date: str, owner_display_name: str = "") -> str:
    owner_part = sanitize_filename_component(owner_display_name)
    if owner_part:
        return f"{target_date}-{owner_part}.md"
    return f"{target_date}.md"


def build_merged_markdown_filename(target_date: str, owner_display_name: str) -> str:
    owner_part = sanitize_filename_component(owner_display_name)
    if not owner_part:
        owner_part = "self"
    return f"{target_date}-{owner_part}-merged.md"


def parse_worktrace_markdown_filename(filename: str) -> ParsedWorktraceFilename:
    path = Path(filename)
    stem = path.stem
    date_match = _DATE_RE.search(stem)
    target_date = date_match.group(1) if date_match else ""
    remaining = stem
    if date_match:
        remaining = f"{stem[:date_match.start()]} {stem[date_match.end():]}"

    is_merged = False
    owner_name = remaining
    while True:
        merged_match = _MERGED_TOKEN_RE.search(owner_name)
        if not merged_match:
            break
        is_merged = True
        owner_name = f"{owner_name[:merged_match.start()]} {owner_name[merged_match.end():]}"
    owner_name = _EDGE_SEPARATOR_RE.sub("", owner_name).strip()

    return ParsedWorktraceFilename(
        filename=filename,
        stem=stem,
        suffix=path.suffix.lower(),
        target_date=target_date,
        owner_name=owner_name,
        is_merged=is_merged,
    )
