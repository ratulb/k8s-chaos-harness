"""Minimal OpenAI-compatible LLM client with health checks."""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import Any, Callable

from common.errors import LLMAttempt, LLMError


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


@dataclass
class Backend:
    name: str
    base_url: str
    model: str
    api_key: str = "ollama"
    priority: int = 100
    fallbacks: tuple["Backend", ...] = ()


class LLMClient:
    def __init__(self, backend: Backend, health_sort: bool | None = None):
        self.backend = backend
        self.health_sort = _env_flag("CHAOS_LLM_HEALTH_SORT", True) if health_sort is None else health_sort
        self._candidate_order: list[Backend] | None = None

    def _raw_candidates(self) -> list[Backend]:
        return [self.backend, *self.backend.fallbacks]

    def _candidates(self) -> list[Backend]:
        if self._candidate_order is None:
            candidates = self._raw_candidates()
            if self.health_sort and len(candidates) > 1:
                statuses = [(backend, self._health_one(backend)) for backend in candidates]
                healthy = [backend for backend, ok in statuses if ok]
                if healthy:
                    self._candidate_order = healthy
                else:
                    self._candidate_order = candidates
            else:
                self._candidate_order = candidates
        return list(self._candidate_order)

    def _url(self, backend: Backend, path: str) -> str:
        base = backend.base_url.rstrip("/")
        if path in ("/chat/completions", "/models") and not base.endswith("/v1"):
            base += "/v1"
        return base + path

    def _health_one(self, backend: Backend) -> bool:
        for method, path, payload in (
            ("GET", "/models", None),
            ("GET", "/api/tags", None),
            ("POST", "/chat/completions", {"model": backend.model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}),
        ):
            try:
                data = None if payload is None else json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(self._url(backend, path), data=data, method=method)
                req.add_header("User-Agent", "k8s-chaos-harness/1.0")
                if backend.api_key:
                    req.add_header("Authorization", f"Bearer {backend.api_key}")
                if data is not None:
                    req.add_header("Content-Type", "application/json")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if 200 <= resp.status < 300:
                        return True
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                continue
        return False

    def health(self) -> bool:
        candidates = self._raw_candidates()
        statuses = [self._health_one(backend) for backend in candidates]
        if any(statuses):
            self._candidate_order = [backend for backend, ok in zip(candidates, statuses) if ok]
        else:
            self._candidate_order = list(candidates)
        return any(statuses)

    def _open_completion(
        self,
        backend: Backend,
        payload: dict[str, Any],
        timeout: int,
        extra_headers: dict[str, str] | None = None,
        retries: int = 8,
    ) -> Any:
        headers = {
            "User-Agent": "k8s-chaos-harness/1.0",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {backend.api_key}",
        }
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(
            self._url(backend, "/chat/completions"),
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        last = None
        for attempt in range(retries + 1):
            try:
                return urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(min(60, 5 * (2 ** min(attempt, 4))) + random.uniform(0, 2))
                    continue
                raise LLMError.from_exception(e) from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                if attempt < retries:
                    time.sleep(min(60, 5 * (2 ** min(attempt, 4))) + random.uniform(0, 2))
                    continue
                raise LLMError.from_exception(e) from e
        if last is not None:
            raise LLMError.from_exception(last)
        raise RuntimeError("completion request failed")

    def _chat_stream(
        self,
        backend: Backend,
        payload: dict[str, Any],
        timeout: int,
        on_chunk: Callable[[str], None],
        retries: int = 8,
    ) -> dict[str, Any]:
        stream_payload = dict(payload)
        stream_payload["stream"] = True
        stream_payload["stream_options"] = {"include_usage": True}
        content_parts: list[str] = []
        usage: dict[str, Any] | None = None
        tool_calls: dict[int, dict[str, Any]] = {}
        with self._open_completion(
            backend,
            stream_payload,
            timeout,
            {"Accept": "text/event-stream"},
            retries=retries,
        ) as resp:
            if not (200 <= getattr(resp, "status", 200) < 300):
                raise RuntimeError(f"streaming request failed with status {resp.status}")
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").strip() if isinstance(raw_line, bytes) else raw_line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("usage"):
                    usage = dict(chunk["usage"])
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta", {}) or {}
                    text = delta.get("content") or ""
                    if text:
                        content_parts.append(text)
                        on_chunk(text)
                    for index, call in enumerate(delta.get("tool_calls", []) or []):
                        function = call.get("function", {}) or {}
                        key = int(call.get("index", index))
                        entry = tool_calls.setdefault(key, {
                            "id": call.get("id") or f"call_{key}",
                            "type": "function",
                            "function": {"name": function.get("name") or "", "arguments": ""},
                        })
                        if function.get("name"):
                            entry["function"]["name"] = function["name"]
                        entry["function"]["arguments"] += function.get("arguments") or ""
        if not content_parts and not tool_calls:
            raise RuntimeError("streaming response was empty")
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_parts),
        }
        if tool_calls:
            message["tool_calls"] = [tool_calls[key] for key in sorted(tool_calls)]
        return {
            "choices": [{"message": message, "finish_reason": "reconstructed"}],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "stream_fallback": False,
        }

    def _chat_once(
        self,
        backend: Backend,
        payload: dict[str, Any],
        timeout: int,
        on_chunk: Callable[[str], None] | None,
        retries: int,
    ) -> dict[str, Any]:
        request_payload = dict(payload)
        request_payload["model"] = backend.model
        if on_chunk is not None:
            chunks: list[str] = []
            try:
                data = self._chat_stream(backend, request_payload, timeout, chunks.append, retries=retries)
            except Exception:
                data = None
            if data is not None:
                for text in chunks:
                    on_chunk(text)
                return data
        with self._open_completion(backend, request_payload, timeout, retries=retries) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        data["stream_fallback"] = on_chunk is not None
        choices = data.get("choices") or []
        if choices and not choices[0].get("finish_reason"):
            choices[0]["finish_reason"] = "reconstructed" if data["stream_fallback"] else "unknown"
        return data

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_chunk: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        timeout = int(kwargs.pop("timeout", 120))
        fallback_mode = len(self._candidates()) > 1
        default_retries = 8
        if fallback_mode:
            try:
                default_retries = int(os.environ.get("CHAOS_LLM_FALLBACK_RETRIES", "0"))
            except ValueError:
                default_retries = 0
        retries = int(kwargs.pop("retries", default_retries))
        max_attempts = int(kwargs.pop("max_attempts", len(self._candidates())))
        kwargs.pop("stream", None)
        payload: dict[str, Any] = {
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
        payload.update(kwargs)

        candidates = self._candidates()[: max(1, max_attempts)]
        raw_candidates = self._raw_candidates()
        attempted_names = {candidate.name for candidate in candidates}
        skipped = [candidate.name for candidate in raw_candidates if candidate.name not in attempted_names]
        attempts: list[LLMAttempt] = []
        last_error: BaseException | None = None
        for backend in candidates:
            buffered: list[str] = []
            buffered_chunk = buffered.append if on_chunk is not None else None
            try:
                data = self._chat_once(backend, payload, timeout, buffered_chunk, retries)
            except Exception as exc:
                last_error = exc
                attempts.append(LLMAttempt(backend.name, backend.model, str(exc)))
                continue
            if on_chunk is not None:
                for text in buffered:
                    on_chunk(text)
            raw_index = next(
                (index for index, item in enumerate(raw_candidates) if item.name == backend.name),
                0,
            )
            data["backend"] = backend.name
            data["model"] = backend.model
            data["attempt"] = raw_index + 1
            data["requested_backend"] = self.backend.name
            data["fallback_from"] = [item.name for item in raw_candidates[:raw_index]]
            data["skipped_backends"] = skipped
            return data
        if last_error is not None:
            error = LLMError.from_exception(last_error)
            raise replace(
                error,
                attempts=tuple(attempts),
                requested_backend=self.backend.name,
                skipped_backends=tuple(skipped),
            ) from last_error
        raise RuntimeError("completion request failed")
