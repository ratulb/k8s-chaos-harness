from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Sequence

from common.errors import LLMError
from common.results import Result, ResultKind
from harness.message import Message

if TYPE_CHECKING:
    from harness.journal import Journal


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
    backend: str | None = None
    model: str | None = None
    attempt: int = 1
    requested_backend: str | None = None
    fallback_from: tuple[str, ...] = ()
    skipped_backends: tuple[str, ...] = ()
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
            backend=value.get("backend"),
            model=value.get("model"),
            attempt=int(value.get("attempt") or 1),
            requested_backend=value.get("requested_backend"),
            fallback_from=tuple(value.get("fallback_from") or ()),
            skipped_backends=tuple(value.get("skipped_backends") or ()),
            raw=value,
        )

    def to_dict(self) -> dict[str, Any]:
        return self.raw

    @property
    def primary(self) -> Message:
        return self.messages[0] if self.messages else Message.assistant("")

    def commit(self, journal: Journal, backend: str, latency_ms: int) -> Message:
        used_backend = self.backend or backend
        journal.store.add_llm_request(
            journal.run_id,
            used_backend,
            latency_ms,
            self.usage.prompt_tokens,
            self.usage.completion_tokens,
            attempt=self.attempt,
        )
        journal.artifact("llm_response_full", Message.redact_payload(self.to_dict()))
        message = self.primary
        message.persist(journal, backend=used_backend, tokens=self.usage.total_tokens)
        journal.event({
            "type": "assistant",
            "message": message.to_stored_dict(),
            "tokens": self.usage.total_tokens,
            "finish_reason": self.finish_reason,
            "stream_fallback": self.stream_fallback,
            "backend": used_backend,
            "attempt": self.attempt,
            "requested_backend": self.requested_backend,
            "fallback_from": list(self.fallback_from),
            "skipped_backends": list(self.skipped_backends),
        })
        return message


@dataclass(frozen=True)
class LLMOutcome:
    result: Result[LLMResponse]
    latency_ms: int
    error: LLMError | None = None

    @classmethod
    def invoke(
        cls,
        client: Any,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        on_chunk: Any = None,
        **kwargs: Any,
    ) -> "LLMOutcome":
        started = time.monotonic()
        try:
            response = LLMResponse.from_value(client.chat(
                list(messages),
                tools=tools,
                on_chunk=on_chunk,
                **kwargs,
            ))
        except Exception as exc:
            error = LLMError.from_exception(exc)
            return cls(
                result=Result.failure(str(error)),
                latency_ms=int((time.monotonic() - started) * 1000),
                error=error,
            )
        return cls(
            result=Result.success(response),
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    @property
    def failed(self) -> bool:
        return self.result.kind is ResultKind.FAILURE

    @property
    def end_reason(self) -> str:
        return self.error.classify() if self.error is not None else "error"

    def commit(self, journal: Journal, backend: str) -> LLMResponse | None:
        if self.failed:
            error_text = self.result.error or str(self.error or "unknown LLM error")
            attempts = self.error.attempts if self.error is not None else ()
            attempted = [
                {"backend": item.backend, "model": item.model, "error": item.error}
                for item in attempts
            ]
            used_backend = attempted[-1]["backend"] if attempted else backend
            journal.store.add_llm_request(journal.run_id, used_backend, self.latency_ms, 0, 0, error_text)
            journal.event({
                "type": "llm_error",
                "error": error_text,
                "backend": used_backend,
                "requested_backend": self.error.requested_backend if self.error else None,
                "attempts": attempted,
                "skipped_backends": list(self.error.skipped_backends) if self.error else [],
            })
            journal.artifact("llm_error_full", {"error": error_text, "backend": used_backend, "attempts": attempted})
            return None
        response = self.result.unwrap()
        response.commit(journal, backend, self.latency_ms)
        return response
