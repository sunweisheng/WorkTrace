from __future__ import annotations

from hashlib import sha1


def stable_event_id(target_date: str, source_message_ids: list[str]) -> str:
    payload = f"{target_date}|{','.join(source_message_ids)}"
    return sha1(payload.encode("utf-8")).hexdigest()[:16]
