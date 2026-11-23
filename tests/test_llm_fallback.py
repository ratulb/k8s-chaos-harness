from __future__ import annotations

import json
import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_benchmark
from llm.client import Backend, LLMClient
from llm.registry import backend_choices, resolve_backend
from llm.response import LLMOutcome, LLMResponse


ENDPOINTS = [
    {"name": "gpu", "base_url": "http://127.0.0.1:11435", "model": "large", "priority": 1},
    {"name": "backup", "base_url": "https://api.example.com/v1", "model": "small", "api_key": "key", "priority": 2},
]


class RecordingStore:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def add_llm_request(self, *args: object, **kwargs: object) -> None:
        self.calls.append((args, kwargs))

    def append_message(self, *args: object, **kwargs: object) -> None:
        self.calls.append((args, kwargs))


class RecordingJournal:
    run_id = "fallback-test"

    def __init__(self) -> None:
        self.store = RecordingStore()
        self.events: list[dict] = []
        self.artifacts: list[tuple[str, object]] = []

    def artifact(self, kind: str, payload: object) -> None:
        self.artifacts.append((kind, payload))

    def event(self, payload: dict) -> None:
        self.events.append(payload)


class TestLlmFallback(unittest.TestCase):
    def test_registry_orders_custom_endpoints_and_attaches_fallbacks(self) -> None:
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

    def test_registry_honors_max_attempts(self) -> None:
        env = {
            "CHAOS_LLM_ENDPOINTS": json.dumps(ENDPOINTS),
            "CHAOS_LLM_MAX_ATTEMPTS": "1",
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
        self.assertEqual(data["attempt"], 2)
        self.assertEqual(data["fallback_from"], ["primary"])
        self.assertEqual(data["skipped_backends"], ["primary"])

    def test_response_commit_records_actual_backend(self) -> None:
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
        journal = RecordingJournal()
        response.commit(journal, "primary", 12)

        self.assertEqual(journal.store.calls[0][0][1], "secondary")
        self.assertEqual(journal.store.calls[0][1]["attempt"], 2)
        self.assertEqual(journal.events[0]["backend"], "secondary")
        self.assertEqual(journal.events[0]["fallback_from"], ["primary"])

    def test_stream_downgrade_discards_partial_chunks(self) -> None:
        backend = Backend("primary", "http://primary", "model", "key")
        client = LLMClient(backend, health_sort=False)
        emitted: list[str] = []

        def fake_stream(self, backend, payload, timeout, on_chunk, retries=8):
            on_chunk("partial")
            raise RuntimeError("stream broke")

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"role": "assistant", "content": "final"}}]}).encode()

        with mock.patch.object(LLMClient, "_chat_stream", fake_stream), mock.patch.object(
            LLMClient, "_open_completion", lambda *args, **kwargs: Response()
        ):
            data = client.chat([{"role": "user", "content": "ping"}], on_chunk=emitted.append)

        self.assertEqual(emitted, [])
        self.assertTrue(data["stream_fallback"])
        self.assertEqual(data["choices"][0]["message"]["content"], "final")

    def test_exhausted_chain_records_failed_attempts(self) -> None:
        primary = Backend("primary", "http://primary", "model", "key")
        secondary = Backend("secondary", "http://secondary", "other", "key")
        client = LLMClient(replace(primary, fallbacks=(secondary,)), health_sort=False)

        def fake_chat_once(self, backend, payload, timeout, on_chunk, retries):
            raise RuntimeError(f"{backend.name} down")

        with mock.patch.object(LLMClient, "_chat_once", fake_chat_once):
            outcome = LLMOutcome.invoke(client, [{"role": "user", "content": "ping"}])

        self.assertTrue(outcome.failed)
        self.assertIsNotNone(outcome.error)
        self.assertEqual(outcome.error.attempted_backends, ("primary", "secondary"))
        journal = RecordingJournal()
        outcome.commit(journal, "primary")
        self.assertEqual(journal.store.calls[0][0][1], "secondary")
        self.assertEqual(journal.events[0]["backend"], "secondary")
        self.assertEqual([item["backend"] for item in journal.events[0]["attempts"]], ["primary", "secondary"])
        self.assertEqual(journal.artifacts[0][0], "llm_error_full")

    def test_resume_uses_resolved_backend_name(self) -> None:
        fixture = json.loads((ROOT / "tests" / "fixtures" / "topologies" / "n3.json").read_text())
        fixture["source"] = "live"
        fixture["topology_hash"] = "test-hash"
        capability = {"eligible": True, "impact_class": "disruptive", "unsupported_reason": None}
        store = mock.MagicMock()
        store.hash_spec.return_value = "spec-hash"
        store.ensure_scenario.return_value = 1
        store.resume_candidates.return_value = []
        backend = Backend("gpu", "http://127.0.0.1:11435", "large", "key")
        argv = [
            "run_benchmark.py",
            "--scenario", "etcd-member-down",
            "--backend", "auto",
            "--resume",
            "--db", str(ROOT / "fallback-resume.sqlite3"),
        ]

        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            run_benchmark.topology_tools, "discover_topology", return_value=fixture
        ), mock.patch.object(run_benchmark.topology_tools, "freeze_topology"), mock.patch.object(
            run_benchmark.topology_tools, "capability_check", return_value=capability
        ), mock.patch.object(
            run_benchmark.topology_tools, "resolve_targets", return_value=["node-b"]
        ), mock.patch.object(
            run_benchmark.safety_tools, "require_destructive_guard", return_value={}
        ), mock.patch.object(run_benchmark, "active_topology_hash", return_value="test-hash"), mock.patch.object(
            run_benchmark, "SQLiteStore", return_value=store
        ), mock.patch.object(run_benchmark, "resolve_backend", return_value=backend), mock.patch.object(
            run_benchmark, "LLMClient"
        ) as client_cls:
            client_cls.return_value.health.return_value = True
            with self.assertRaises(SystemExit) as ctx:
                run_benchmark.main()

        self.assertEqual(ctx.exception.code, 1)
        store.resume_candidates.assert_called_once_with("etcd-member-down", "gpu")
        config_input = store.hash_spec.call_args_list[-1].args[0]
        self.assertEqual(config_input["backend_selector"], "auto")
        self.assertEqual([item["name"] for item in config_input["fallback_chain"]], ["gpu"])

    def test_registry_rejects_reserved_custom_endpoint_name(self) -> None:
        specs = [{"name": "auto", "base_url": "http://127.0.0.1:11435", "model": "large"}]
        env = {"CHAOS_LLM_ENDPOINTS": json.dumps(specs)}
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError) as ctx:
            backend_choices()
        self.assertIn("reserved or duplicate name 'auto'", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
