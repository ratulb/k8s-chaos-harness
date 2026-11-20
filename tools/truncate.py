from __future__ import annotations

DEFAULT_LIMIT = 3000


def truncate(text: str, limit: int = DEFAULT_LIMIT) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text)-limit} chars]"
