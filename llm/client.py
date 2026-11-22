"""Minimal OpenAI-compatible LLM client with health checks."""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from common.errors import LLMError


@dataclass
class Backend:
    name: str
    base_url: str
    model: str
    api_key: str = "ollama"


class LLMClient:
    def __init__(self, backend: Backend):
        self.backend = backend

    def _url(self, path: str) -> str:
        base = self.backend.base_url.rstrip("/")
        if path in ("/chat/completions", "/models") and not base.endswith("/v1"):
            base += "/v1"
        return base + path

    def health(self) -> bool:
        # Try common read endpoints first.
        for method, path, payload in (
            ("GET", "/models", None),
            ("GET", "/api/tags", None),
            ("POST", "/chat/completions", {"model": self.backend.model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}),
        ):
            try:
                data = None if payload is None else json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(self._url(path), data=data, method=method)
                req.add_header("User-Agent", "k8s-chaos-harness/1.0")
                if self.backend.api_key:
                    req.add_header("Authorization", f"Bearer {self.backend.api_key}")
                if data is not None:
                    req.add_header("Content-Type", "application/json")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if 200 <= resp.status < 300:
                        return True
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                continue
        return False

    def _open_completion(
        self,
        payload: dict[str, Any],
        timeout: int,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        headers = {
            "User-Agent": "k8s-chaos-harness/1.0",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.backend.api_key}",
        }
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(
            self._url("/chat/completions"),
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        last = None
        for attempt in range(9):
            try:
                return urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (429, 500, 502, 503, 504) and attempt < 8:
                    time.sleep(min(60, 5 * (2 ** min(attempt, 4))) + random.uniform(0, 2))
                    continue
                raise LLMError.from_exception(e) from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                if attempt < 8:
                    time.sleep(min(60, 5 * (2 ** min(attempt, 4))) + random.uniform(0, 2))
                    continue
                raise LLMError.from_exception(e) from e
        if last is not None:
            raise LLMError.from_exception(last)
        raise RuntimeError("completion request failed")

    def _chat_stream(
        self,
        payload: dict[str, Any],
        timeout: int,
        on_chunk: Callable[[str], None],
    ) -> dict[str, Any]:
        stream_payload = dict(payload)
        stream_payload["stream"] = True
        stream_payload["stream_options"] = {"include_usage": True}
        content_parts: list[str] = []
        usage: dict[str, Any] | None = None
        tool_calls: dict[int, dict[str, Any]] = {}
        with self._open_completion(
            stream_payload, timeout, {"Accept": "text/event-stream"}
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

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_chunk: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        timeout = int(kwargs.pop("timeout", 120))
        kwargs.pop("stream", None)
        payload: dict[str, Any] = {
            "model": self.backend.model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
        payload.update(kwargs)

        if on_chunk is not None:
            try:
                return self._chat_stream(payload, timeout, on_chunk)
            except Exception:
                pass
        with self._open_completion(payload, timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        data["stream_fallback"] = on_chunk is not None
        choices = data.get("choices") or []
        if choices and not choices[0].get("finish_reason"):
            choices[0]["finish_reason"] = "reconstructed" if data["stream_fallback"] else "unknown"
        return data
