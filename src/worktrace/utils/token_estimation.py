from __future__ import annotations


def estimate_text_tokens(value: str) -> int:
    return max(1, len(value) // 3 + 50)
