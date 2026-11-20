"""Minimal OpenAI-compatible LLM client with health checks."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


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
                req.add_header("User-Agent", "etcd-chaos-bench/1.0")
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

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, **kwargs: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.backend.model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
        payload.update(kwargs)

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._url("/chat/completions"),
            data=data,
            headers={
                "User-Agent": "etcd-chaos-bench/1.0",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.backend.api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=kwargs.get("timeout", 120)) as resp:
            return json.loads(resp.read().decode("utf-8"))
