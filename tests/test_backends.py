#!/usr/bin/env python3
"""Smoke test: every configured backend must answer a trivial prompt.

This verifies credentials, base URLs, model names, and the OpenAI-compatible
response shape without depending on the full benchmark harness.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from llm.env import load_env

load_env()

from llm.client import Backend, LLMClient
from llm.registry import all_backends


def configured_backends() -> list[Backend]:
    return all_backends()


class TestBackendDiscovery(unittest.TestCase):
    def test_custom_endpoints_are_included(self) -> None:
        model = "hf.co/prism-ml/Ternary-Bonsai-2.5-27B-GGUF:Q2_0"
        endpoints = [{
            "name": "bonsai",
            "base_url": "http://127.0.0.1:11436",
            "model": model,
            "priority": 5,
        }]
        env = {
            "CHAOS_LLM_ENDPOINTS": json.dumps(endpoints),
            "OLLAMA_BASE_URL": "",
            "GROQ_API_KEY": "",
            "OPENROUTER_API_KEY": "",
            "CLOUDFLARE_ACCOUNT_ID": "",
            "CLOUDFLARE_AUTH_TOKEN": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            backends = configured_backends()
        matches = [backend for backend in backends if backend.name == "bonsai"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].base_url, "http://127.0.0.1:11436")
        self.assertEqual(matches[0].model, model)


class TestBackends(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Skip cleanly when no provider credentials are present; this keeps
        # offline development and CI without secrets green.
        cls.backends = configured_backends()
        if os.environ.get("CHAOS_LIVE_BACKEND_TESTS") != "1":
            raise unittest.SkipTest("Set CHAOS_LIVE_BACKEND_TESTS=1 for live backend smoke tests")
        if not cls.backends:
            raise unittest.SkipTest("No backends configured in .env")

    def test_each_backend_responds(self) -> None:
        # A backend must both pass its health probe and return a non-empty
        # assistant message for a trivial prompt. subTest isolates failures so
        # one broken provider does not hide the status of the others.
        for backend in self.backends:
            with self.subTest(backend=backend.name):
                client = LLMClient(backend)
                self.assertTrue(client.health(), f"{backend.name} health check failed")

                # temperature=0 and a tiny max_tokens make this cheap and
                # deterministic; reasoning-only models may put text in either
                # 'content' or 'reasoning', so accept both fields.
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
