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
        return self.backend.base_url.rstrip("/") + path

    def health(self) -> bool:
        # Try OpenAI-compatible and Ollama-native endpoints.
        for path in ("/models", "/api/tags"):
            try:
                req = urllib.request.Request(self._url(path))
                if self.backend.api_key:
                    req.add_header("Authorization", f"Bearer {self.backend.api_key}")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        return True
            except (urllib.error.URLError, TimeoutError, OSError):
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
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.backend.api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=kwargs.get("timeout", 120)) as resp:
            return json.loads(resp.read().decode("utf-8"))
