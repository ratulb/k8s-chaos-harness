from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.journal import Journal
from llm.client import Backend, LLMClient
from llm.response import LLMOutcome
from storage.sqlite_store import SQLiteStore


class _Handler(BaseHTTPRequestHandler):
    mode = "healthy"
    name = "mock"

    def log_message(self, *args: object) -> None:
        return

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ("/v1/models", "/models"):
            self._send(200, {"object": "list", "data": [{"id": self.name}]})
        elif self.path == "/api/tags":
            self._send(200, {"models": [{"name": self.name}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            self._send(404, {"error": "not found"})
            return
        if self.mode == "chat500":
            self._send(500, {"error": {"message": "forced chat failure"}})
            return
        self._send(200, {
            "id": "chatcmpl-e2e",
            "object": "chat.completion",
            "model": self.name,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "pong"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        })


_SERVER_THREADS: list[threading.Thread] = []


def _start_server(mode: str, name: str) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"mode": mode, "name": name})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    _SERVER_THREADS.append(thread)
    thread.start()
    return server


class TestLlmFallbackE2E(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.good = _start_server("healthy", "mock-good")
        self.bad = _start_server("chat500", "mock-bad")
        self.store = SQLiteStore(Path(self.tmp.name) / "e2e.sqlite3")
        spec = {"id": "etcd-member-down", "root_cause": "etcd_member_unreachable"}
        spec_hash = self.store.hash_spec(spec)
        version = self.store.ensure_scenario(spec["id"], spec_hash, json.dumps(spec))
        self.store.create_run(
            "e2e-run",
            scenario_id=spec["id"],
            scenario_version=version,
            spec_hash=spec_hash,
            backend="mock-bad",
            truth=spec["root_cause"],
            status="running",
            phase="agent_running",
        )
        self.journal = Journal(run_id="e2e-run", store=self.store)

    def tearDown(self) -> None:
        self.store.close()
        for server in (self.good, self.bad):
            server.shutdown()
            server.server_close()
        for thread in _SERVER_THREADS:
            thread.join(timeout=2)
        self.tmp.cleanup()

    def _url(self, server: ThreadingHTTPServer) -> str:
        return f"http://127.0.0.1:{server.server_port}"

    def test_fallback_commits_actual_backend_to_sqlite(self) -> None:
        primary = Backend("mock-bad", self._url(self.bad), "fake-bad", "key")
        secondary = Backend("mock-good", self._url(self.good), "fake", "key")
        client = LLMClient(replace(primary, fallbacks=(secondary,)), health_sort=False)
        outcome = LLMOutcome.invoke(client, [{"role": "user", "content": "ping"}])

        self.assertFalse(outcome.failed)
        response = outcome.commit(self.journal, "mock-bad")
        self.assertIsNotNone(response)
        self.assertEqual(response.backend, "mock-good")
        self.assertEqual(response.attempt, 2)

        with self.store._connect() as conn:
            requests = [dict(row) for row in conn.execute(
                "SELECT backend, attempt FROM llm_requests WHERE run_id=? ORDER BY req_id", ("e2e-run",)
            )]
            events = [dict(row) for row in conn.execute(
                "SELECT payload_json FROM trace_events WHERE run_id=? AND type='assistant'", ("e2e-run",)
            )]
            artifacts = [dict(row) for row in conn.execute(
                "SELECT content FROM run_artifacts WHERE run_id=? AND kind='llm_response_full'", ("e2e-run",)
            )]
        self.assertEqual(requests, [{"backend": "mock-good", "attempt": 2}])
        event = json.loads(events[0]["payload_json"])
        self.assertEqual(event["backend"], "mock-good")
        self.assertEqual(event["fallback_from"], ["mock-bad"])
        artifact = json.loads(artifacts[0]["content"])
        self.assertEqual(artifact["backend"], "mock-good")

    def test_exhausted_chain_commits_attempt_records(self) -> None:
        first = Backend("mock-bad", self._url(self.bad), "fake-bad", "key")
        second = Backend("mock-bad-2", self._url(self.bad), "fake-bad", "key")
        client = LLMClient(replace(first, fallbacks=(second,)), health_sort=False)
        outcome = LLMOutcome.invoke(client, [{"role": "user", "content": "ping"}])

        self.assertTrue(outcome.failed)
        self.assertIsNone(outcome.commit(self.journal, "mock-bad"))

        with self.store._connect() as conn:
            requests = [dict(row) for row in conn.execute(
                "SELECT backend, error FROM llm_requests WHERE run_id=?", ("e2e-run",)
            )]
            events = [dict(row) for row in conn.execute(
                "SELECT payload_json FROM trace_events WHERE run_id=? AND type='llm_error'", ("e2e-run",)
            )]
            artifacts = [dict(row) for row in conn.execute(
                "SELECT content FROM run_artifacts WHERE run_id=? AND kind='llm_error_full'", ("e2e-run",)
            )]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["backend"], "mock-bad-2")
        event = json.loads(events[0]["payload_json"])
        self.assertEqual(event["backend"], "mock-bad-2")
        self.assertEqual([item["backend"] for item in event["attempts"]], ["mock-bad", "mock-bad-2"])
        artifact = json.loads(artifacts[0]["content"])
        self.assertEqual(artifact["backend"], "mock-bad-2")

    def test_health_sort_skips_dead_candidate_over_http(self) -> None:
        dead = Backend("dead", "http://127.0.0.1:1", "x", "key")
        good = Backend("mock-good", self._url(self.good), "fake", "key")
        client = LLMClient(replace(dead, fallbacks=(good,)), health_sort=True)
        outcome = LLMOutcome.invoke(client, [{"role": "user", "content": "ping"}])

        self.assertFalse(outcome.failed)
        response = outcome.result.unwrap()
        self.assertEqual(response.backend, "mock-good")
        self.assertEqual(response.fallback_from, ("dead",))
        self.assertEqual(response.skipped_backends, ("dead",))


if __name__ == "__main__":
    unittest.main(verbosity=2)
