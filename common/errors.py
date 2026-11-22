from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LLMError(Exception):
    kind: str
    message: str
    status_code: int | None = None

    def __str__(self) -> str:
        return self.message

    @property
    def end_reason(self) -> str:
        if self.status_code == 413:
            return "provider_error_413"
        if self.status_code == 429:
            return "provider_error_429"
        if self.status_code in {500, 502, 503, 504}:
            return "provider_error_5xx"
        return "error"

    @classmethod
    def from_exception(cls, error: BaseException) -> "LLMError":
        if isinstance(error, cls):
            return error
        status_code = _status_code(error)
        if status_code is not None:
            return cls(kind="http", message=str(error), status_code=status_code)
        kind = "timeout" if isinstance(error, TimeoutError) else "client"
        return cls(kind=kind, message=str(error))


def _status_code(error: BaseException) -> int | None:
    value: Any = getattr(error, "code", None)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def classify_llm_error(error: BaseException | str) -> str:
    if isinstance(error, LLMError):
        return error.end_reason
    status_code = _status_code(error) if isinstance(error, BaseException) else None
    if status_code is not None:
        return LLMError(kind="http", message=str(error), status_code=status_code).end_reason
    text = str(error).lower()
    if "413" in text or "payload too large" in text:
        return "provider_error_413"
    if "429" in text or "too many requests" in text:
        return "provider_error_429"
    if any(code in text for code in ("500", "502", "503", "504")):
        return "provider_error_5xx"
    return "error"
