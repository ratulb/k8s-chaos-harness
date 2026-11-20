#!/usr/bin/env python3
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import harness.agent as agent


class FakeBackend:
    name = "fake"


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)

    def chat(self, messages, **kwargs):
        return self.responses.pop(0)


def assistant(tool_calls):
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": tool_calls}}], "usage": {"total_tokens": 10}}


def tool_call(name, args):
    return {"id": "call-1", "function": {"name": name, "arguments": __import__("json").dumps(args)}}


class TestRepairGate(unittest.TestCase):
    def test_forbidden_repair_blocked_and_logged(self):
        responses = [
            assistant([tool_call("submit_diagnosis", {"root_cause": "etcd_member_unreachable"})]),
            assistant([tool_call("reset_cluster", {"approval_token": "approved:gate-test"})]),
            assistant([]),
        ]
        original = agent.LLMClient
        agent.LLMClient = lambda backend: FakeClient(responses)
        try:
            with tempfile.TemporaryDirectory() as d:
                rec = agent.run_scenario(
                    {
                        "id": "gate-test",
                        "root_cause": "etcd_member_unreachable",
                        "acceptable_repairs": ["restart_member"],
                        "forbidden_repairs": ["reset_cluster"],
                    },
                    FakeBackend(),
                    "gate-test",
                    "true",
                    "true",
                    trace_dir=d,
                )
        finally:
            agent.LLMClient = original

        self.assertTrue(rec["approval_granted"])
        self.assertTrue(rec["diagnosis_correct"])
        self.assertIsNone(rec["repair"])
        self.assertIn("forbidden", rec["forbidden_reason"])
        self.assertTrue(any("forbidden" in v for v in rec["policy_violations"]))

    def test_repair_before_diagnosis_blocked(self):
        responses = [
            assistant([tool_call("restart_member", {"node": "vm2"})]),
            assistant([]),
        ]
        original = agent.LLMClient
        agent.LLMClient = lambda backend: FakeClient(responses)
        try:
            with tempfile.TemporaryDirectory() as d:
                rec = agent.run_scenario(
                    {
                        "id": "gate-test-2",
                        "root_cause": "etcd_member_unreachable",
                        "acceptable_repairs": ["restart_member"],
                        "forbidden_repairs": [],
                    },
                    FakeBackend(),
                    "gate-test-2",
                    "true",
                    "true",
                    trace_dir=d,
                )
        finally:
            agent.LLMClient = original

        self.assertFalse(rec["approval_granted"])
        self.assertIsNone(rec["repair"])
        self.assertTrue(any("diagnosis required" in v for v in rec["policy_violations"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
