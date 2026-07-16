from __future__ import annotations

from hashlib import sha1, sha256
from urllib.parse import urlsplit, urlunsplit


def stable_event_id(
    target_date: str,
    source_message_ids: list[str],
    content: str = "",
) -> str:
    payload = f"{target_date}|{','.join(source_message_ids)}|{content}"
    return sha1(payload.encode("utf-8")).hexdigest()[:16]


def evidence_fingerprint(message_id: str) -> str:
    return _namespaced_sha256("worktrace:evidence:v1", message_id)


def conversation_fingerprint(target_date: str, conversation_id: str) -> str:
    cleaned_date = target_date.strip()
    cleaned_conversation_id = conversation_id.strip()
    if not cleaned_date or not cleaned_conversation_id:
        return ""
    return _namespaced_sha256(
        "worktrace:conversation:v1",
        f"{cleaned_date}:{cleaned_conversation_id}",
    )


def file_key_from_url(url: str) -> str:
    normalized = normalize_file_url(url)
    if not normalized:
        return ""
    return _namespaced_sha256("worktrace:file-url:v1", normalized)


def file_key_from_attachment_id(attachment_id: str) -> str:
    cleaned = attachment_id.strip()
    if not cleaned:
        return ""
    return _namespaced_sha256("worktrace:attachment:v1", cleaned)


def normalize_file_url(url: str) -> str:
    cleaned = url.strip()
    if not cleaned:
        return ""
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return urlunsplit(
        (
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            parts.path or "/",
            "",
            "",
        )
    )


def is_sha256_fingerprint(value: str) -> bool:
    if not value.startswith("sha256:") or len(value) != 71:
        return False
    return all(char in "0123456789abcdef" for char in value[7:])


def _namespaced_sha256(namespace: str, value: str) -> str:
    digest = sha256(f"{namespace}:{value}".encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
