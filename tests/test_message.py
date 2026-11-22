#!/usr/bin/env python3
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.message import Message


class TestMessage(unittest.TestCase):
    def test_redact_removes_approval_token_without_mutating_original(self) -> None:
        arguments = json.dumps({"approval_token": "approved:run-1", "node": "vm1"})
        message = {"role": "assistant", "tool_calls": [{"function": {"arguments": arguments}}]}
        redacted = Message.redact(message)
        redacted_args = json.loads(redacted["tool_calls"][0]["function"]["arguments"])
        original_args = json.loads(message["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(redacted_args, {"node": "vm1"})
        self.assertIn("approval_token", original_args)

    def test_truncate_message_content_without_mutating_original(self) -> None:
        message = Message.tool("x" * 5000, "call-1").to_dict()
        truncated = Message.truncate(message, 10)
        self.assertEqual(truncated["content"], "x" * 10 + "\n...[truncated 4990 chars]")
        self.assertEqual(len(message["content"]), 5000)

    def test_round_trip_preserves_role_tool_calls_and_tool_call_id(self) -> None:
        tool_calls = [{"id": "call-1", "function": {"name": "probe", "arguments": "{}"}}]
        message = Message.assistant("", tool_calls)
        self.assertEqual(Message.from_dict(message.to_dict()), message)
        tool_message = Message.tool("ok", "call-1")
        self.assertEqual(Message.from_dict(tool_message.to_dict()), tool_message)


if __name__ == "__main__":
    unittest.main()
