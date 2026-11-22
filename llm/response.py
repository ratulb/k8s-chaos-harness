from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from harness.message import Message


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class LLMResponse:
    messages: tuple[Message, ...]
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None
    stream_fallback: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_value(cls, value: "LLMResponse | dict[str, Any]") -> "LLMResponse":
        if isinstance(value, LLMResponse):
            return value
        choice = (value.get("choices") or [{}])[0]
        usage = value.get("usage") or {}
        total = int(
            usage.get("total_tokens")
            or int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
        )
        return cls(
            messages=tuple(Message.from_dict(item) for item in [choice.get("message") or {}]),
            usage=Usage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=total,
            ),
            finish_reason=choice.get("finish_reason"),
            stream_fallback=bool(value.get("stream_fallback")),
            raw=value,
        )

    def to_dict(self) -> dict[str, Any]:
        return self.raw
