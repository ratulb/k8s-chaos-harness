from __future__ import annotations

import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from llm.client import Backend, LLMClient
from llm.registry import backend_choices, resolve_backend
from llm.response import LLMResponse


ENDPOINTS = [
    {"name": "gpu", "base_url": "http://127.0.0.1:11435", "model": "large", "priority": 1},
    {"name": "backup", "base_url": "https://api.example.com/v1", "model": "small", "api_key": "key", "priority": 2},
]


class TestLlmFallback(unittest.TestCase):
    def test_registry_orders_custom_endpoints_and_attaches_fallbacks(self) -> None:
        import json

        env = {
            "CHAOS_LLM_ENDPOINTS": json.dumps(ENDPOINTS),
            "CHAOS_LLM_FALLBACK": "1",
            "OLLAMA_BASE_URL": "",
            "GROQ_API_KEY": "",
            "OPENROUTER_API_KEY": "",
            "CLOUDFLARE_ACCOUNT_ID": "",
            "CLOUDFLARE_AUTH_TOKEN": "",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            auto = resolve_backend("auto")
            self.assertEqual(auto.name, "gpu")
            self.assertEqual([backend.name for backend in auto.fallbacks], ["backup"])
            self.assertEqual(backend_choices(), ["auto", "gpu", "backup"])

            explicit = resolve_backend("backup")
            self.assertEqual(explicit.name, "backup")
            self.assertEqual([backend.name for backend in explicit.fallbacks], ["gpu"])

    def test_registry_can_disable_fallback(self) -> None:
        import json

        env = {
            "CHAOS_LLM_ENDPOINTS": json.dumps(ENDPOINTS),
            "CHAOS_LLM_FALLBACK": "0",
            "OLLAMA_BASE_URL": "",
            "GROQ_API_KEY": "",
            "OPENROUTER_API_KEY": "",
            "CLOUDFLARE_ACCOUNT_ID": "",
            "CLOUDFLARE_AUTH_TOKEN": "",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            backend = resolve_backend("auto")
        self.assertEqual(backend.name, "gpu")
        self.assertEqual(backend.fallbacks, ())

    def test_chat_uses_next_backend_and_replays_only_successful_chunks(self) -> None:
        primary = Backend("primary", "http://primary", "model", "key")
        secondary = Backend("secondary", "http://secondary", "model", "key")
        primary = replace(primary, fallbacks=(secondary,))
        client = LLMClient(primary, health_sort=False)
        chunks: list[str] = []

        def fake_chat_once(self, backend, payload, timeout, on_chunk, retries):
            if backend.name == "primary":
                if on_chunk is not None:
                    on_chunk("discard me")
                raise RuntimeError("primary down")
            if on_chunk is not None:
                on_chunk("pong")
            return {
                "choices": [{"message": {"role": "assistant", "content": "pong"}}],
                "usage": {"total_tokens": 1},
            }

        with mock.patch.object(LLMClient, "_chat_once", fake_chat_once):
            data = client.chat([{"role": "user", "content": "ping"}], on_chunk=chunks.append)

        self.assertEqual(data["backend"], "secondary")
        self.assertEqual(data["fallback_from"], ["primary"])
        self.assertEqual(data["requested_backend"], "primary")
        self.assertEqual(data["attempt"], 2)
        self.assertEqual(chunks, ["pong"])

    def test_health_sort_skips_dead_primary_and_records_it(self) -> None:
        primary = Backend("primary", "http://primary", "model", "key")
        secondary = Backend("secondary", "http://secondary", "model", "key")
        primary = replace(primary, fallbacks=(secondary,))
        client = LLMClient(primary, health_sort=True)

        def fake_health(self, backend):
            return backend.name == "secondary"

        def fake_chat_once(self, backend, payload, timeout, on_chunk, retries):
            return {
                "choices": [{"message": {"role": "assistant", "content": "pong"}}],
                "usage": {"total_tokens": 1},
            }

        with mock.patch.object(LLMClient, "_health_one", fake_health), mock.patch.object(
            LLMClient, "_chat_once", fake_chat_once
        ):
            data = client.chat([{"role": "user", "content": "ping"}])

        self.assertEqual(data["backend"], "secondary")
        self.assertEqual(data["requested_backend"], "primary")
        self.assertEqual(data["skipped_backends"], ["primary"])

    def test_response_commit_records_actual_backend(self) -> None:
        class Store:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            def add_llm_request(self, *args, **kwargs) -> None:
                self.calls.append((args, kwargs))

            def append_message(self, *args, **kwargs) -> None:
                self.calls.append((args, kwargs))

        class Journal:
            run_id = "fallback-test"

            def __init__(self) -> None:
                self.store = Store()
                self.events: list[dict] = []
                self.artifacts: list[tuple[str, object]] = []

            def artifact(self, kind: str, payload: object) -> None:
                self.artifacts.append((kind, payload))

            def event(self, payload: dict) -> None:
                self.events.append(payload)

        response = LLMResponse.from_value({
            "choices": [{"message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "backend": "secondary",
            "model": "small",
            "attempt": 2,
            "requested_backend": "primary",
            "fallback_from": ["primary"],
            "skipped_backends": [],
        })
        journal = Journal()
        response.commit(journal, "primary", 12)

        self.assertEqual(journal.store.calls[0][0][1], "secondary")
        self.assertEqual(journal.store.calls[0][1]["attempt"], 2)
        self.assertEqual(journal.events[0]["backend"], "secondary")
        self.assertEqual(journal.events[0]["fallback_from"], ["primary"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
