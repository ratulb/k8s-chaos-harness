#!/usr/bin/env python3
"""Smoke test: every configured backend must answer a trivial prompt."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from llm.env import load_env

load_env()

from llm.client import Backend, LLMClient


def configured_backends() -> list[Backend]:
    backends: list[Backend] = []

    if os.environ.get("OLLAMA_BASE_URL"):
        backends.append(
            Backend(
                name="ollama",
                base_url=os.environ["OLLAMA_BASE_URL"],
                model=os.environ.get("OLLAMA_MODEL", "qwen3.8:27b"),
                api_key=os.environ.get("OLLAMA_API_KEY", "ollama"),
            )
        )

    if os.environ.get("GROQ_API_KEY"):
        backends.append(
            Backend(
                name="groq",
                base_url="https://api.groq.com/openai/v1",
                model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
                api_key=os.environ["GROQ_API_KEY"],
            )
        )

    if os.environ.get("OPENROUTER_API_KEY"):
        backends.append(
            Backend(
                name="openrouter",
                base_url="https://openrouter.ai/api/v1",
                model=os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"),
                api_key=os.environ["OPENROUTER_API_KEY"],
            )
        )

    cf_account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    cf_token = os.environ.get("CLOUDFLARE_AUTH_TOKEN")
    if cf_account and cf_token:
        backends.append(
            Backend(
                name="cloudflare",
                base_url=f"https://api.cloudflare.com/client/v4/accounts/{cf_account}/ai/v1",
                model=os.environ.get("CLOUDFLARE_MODEL", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"),
                api_key=cf_token,
            )
        )

    return backends


class TestBackends(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.backends = configured_backends()
        if not cls.backends:
            raise unittest.SkipTest("No backends configured in .env")

    def test_each_backend_responds(self) -> None:
        for backend in self.backends:
            with self.subTest(backend=backend.name):
                client = LLMClient(backend)
                self.assertTrue(client.health(), f"{backend.name} health check failed")
                response = client.chat(
                    [{"role": "user", "content": "Reply with exactly: OK"}],
                    temperature=0,
                    max_tokens=128,
                )
                message = response["choices"][0]["message"]
                content = message.get("content") or message.get("reasoning") or ""
                self.assertTrue(str(content).strip(), f"{backend.name} returned empty content")


if __name__ == "__main__":
    unittest.main(verbosity=2)
