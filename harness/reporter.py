from __future__ import annotations

import re
from collections import deque
from typing import Any

from tools.truncate import truncate

_SECRET_FIELD = re.compile(
    r"(?i)[\"']?\b(approval_token|api_key|access_token|auth_token|token|password|secret|authorization)\b[\"']?"
    r"\s*[:=]\s*['\"]?([^,\s;'\"}\)]+)"
)
_BEARER = re.compile(r"(?i)Bearer\s+[A-Za-z0-9._\-]+")


def redact(text: str) -> str:
    cleaned = _SECRET_FIELD.sub(r"\1=<redacted>", text)
    return _BEARER.sub("Bearer <redacted>", cleaned)


class UiReporter:
    def __init__(self, run_id: str, store: Any | None = None, max_chars: int = 2000):
        self.run_id = run_id
        self.store = store
        self.max_chars = max_chars
        self.recent: deque[dict[str, str]] = deque(maxlen=50)

    def emit(self, kind: str, text: Any) -> int:
        payload = redact(str(text))
        payload = truncate(payload, self.max_chars)
        self.recent.append({"kind": str(kind), "text": payload})
        if self.store is None:
            return 0
        try:
            return int(self.store.append_ui_chunk(self.run_id, str(kind), payload))
        except Exception:
            return 0

    def lines(self) -> list[dict[str, str]]:
        return list(self.recent)
