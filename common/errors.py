from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LLMAttempt:
    backend: str
    model: str
    error: str


@dataclass(frozen=True)
class LLMError(Exception):
    kind: str
    message: str
    status_code: int | None = None
    attempts: tuple[LLMAttempt, ...] = ()
    requested_backend: str | None = None
    skipped_backends: tuple[str, ...] = ()

    def __str__(self) -> str:
        return self.message

    @property
    def attempted_backends(self) -> tuple[str, ...]:
        return tuple(item.backend for item in self.attempts)

    def classify(self) -> str:
        if self.status_code == 413:
            return "provider_error_413"
        if self.status_code == 429:
            return "provider_error_429"
        if self.status_code in {500, 502, 503, 504}:
            return "provider_error_5xx"
        return "error"

    @property
    def end_reason(self) -> str:
        return self.classify()

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
        return error.classify()
    status_code = _status_code(error) if isinstance(error, BaseException) else None
    if status_code is not None:
        return LLMError(kind="http", message=str(error), status_code=status_code).classify()
    text = str(error).lower()
    if "413" in text or "payload too large" in text:
        return "provider_error_413"
    if "429" in text or "too many requests" in text:
        return "provider_error_429"
    if any(code in text for code in ("500", "502", "503", "504")):
        return "provider_error_5xx"
    return "error"
