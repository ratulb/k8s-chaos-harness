from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from llm.client import Backend, LLMClient


class FakeResponse:
    def __init__(self, lines: list[str] | None = None, payload: dict | None = None, broken: bool = False):
        self.lines = lines or []
        self.payload = payload or {}
        self.broken = broken
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        if self.broken:
            raise ValueError("broken stream")
        return iter(self.lines)

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def sse(payload: dict) -> str:
    return "data: " + json.dumps(payload) + "\n\n"


class TestLlmStreaming(unittest.TestCase):
    def setUp(self) -> None:
        self.client = LLMClient(Backend(name="ollama", base_url="http://localhost:11434", model="fake"))

    def test_stream_accumulates_content_and_tool_arguments(self) -> None:
        lines = [
            sse({"choices": [{"delta": {"content": "Hel"}}]}),
            sse({"choices": [{"delta": {"content": "lo"}}]}),
            sse({"choices": [{"delta": {"role": "assistant", "tool_calls": [
                {"index": 0, "id": "call-1", "function": {"name": "submit_diagnosis", "arguments": "{\"root_cau"}}
            ]}}]}),
            sse({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": "se\": \"no_fault\"}"}}
            ]}}]}),
            "data: [DONE]\n\n",
        ]
        chunks: list[str] = []
        with patch("llm.client.urllib.request.urlopen", return_value=FakeResponse(lines=lines)):
            response = self.client.chat([{"role": "user", "content": "hi"}], on_chunk=chunks.append)
        self.assertEqual("".join(chunks), "Hello")
        message = response["choices"][0]["message"]
        self.assertEqual(message["content"], "Hello")
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "submit_diagnosis")
        self.assertEqual(json.loads(message["tool_calls"][0]["function"]["arguments"]), {"root_cause": "no_fault"})

    def test_stream_failure_falls_back_to_non_stream(self) -> None:
        fallback = {"choices": [{"message": {"role": "assistant", "content": "fallback"}}]}
        chunks: list[str] = []
        with patch("llm.client.urllib.request.urlopen", side_effect=[FakeResponse(broken=True), FakeResponse(payload=fallback)]):
            response = self.client.chat([{"role": "user", "content": "hi"}], on_chunk=chunks.append)
        self.assertEqual(response["choices"][0]["message"]["content"], "fallback")
        self.assertEqual(chunks, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
