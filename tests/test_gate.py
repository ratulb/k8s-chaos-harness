#!/usr/bin/env python3
"""Unit tests for the diagnosis-to-repair approval gate.

We replace LLMClient with a scripted fake so gate behavior is deterministic and
does not touch the cluster or consume backend tokens. Each test asserts that an
unauthorized repair never reaches the repair implementation and is recorded in
the run record for scoring.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import harness.agent as agent
from harness.prompt import SystemPrompt
from tools import cluster_config, repairs


class FakeBackend:
    """Minimal backend stub: the agent only reads the .name attribute."""
    name = "fake"


class FakeClient:
    """Return pre-scripted assistant messages instead of calling an LLM."""

    def __init__(self, responses):
        self.responses = list(responses)

    def chat(self, messages, **kwargs):
        return self.responses.pop(0)


def assistant(tool_calls):
    """Wrap tool calls in the OpenAI-compatible response shape."""
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": tool_calls}}], "usage": {"total_tokens": 10}}


def tool_call(name, args):
    """Build one function-call object with JSON-encoded arguments."""
    return {"id": "call-1", "function": {"name": name, "arguments": json.dumps(args)}}


class TestRepairGate(unittest.TestCase):
    def test_forbidden_repair_blocked_and_logged(self):
        # Scenario accepts restart_member but explicitly forbids reset_cluster.
        # The fake agent first submits the correct diagnosis (which grants an
        # approval token), then immediately tries the forbidden repair.
        responses = [
            assistant([tool_call("submit_diagnosis", {"root_cause": "etcd_member_unreachable"})]),
            assistant([tool_call("reset_cluster", {})]),
            assistant([]),
        ]
        original = agent.LLMClient
        agent.LLMClient = lambda backend: FakeClient(responses)
        cluster_config.nodes.cache_clear()
        try:
            with patch.dict(os.environ, {"K8S_NODES": "node-a,node-b,node-c"}):
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
                    with open(rec["trace"], encoding="utf-8") as fh:
                        trace_text = fh.read()
        finally:
            agent.LLMClient = original
            cluster_config.nodes.cache_clear()

        # Approval and diagnosis must be recognized, but the forbidden call
        # must not produce a repair and must leave both audit fields populated.
        self.assertTrue(rec["approval_granted"])
        self.assertTrue(rec["diagnosis_correct"])
        self.assertIsNone(rec["repair"])
        self.assertIn("forbidden", rec["forbidden_reason"])
        self.assertTrue(any("forbidden" in v for v in rec["policy_violations"]))
        self.assertIn("Stop attempting repairs", trace_text)
        self.assertIn("escalate to the operator", trace_text)

    def test_repair_before_diagnosis_blocked(self):
        # No node names are hardcoded: resolve a synthetic role through the
        # same portable config resolver used by the production harness.
        cluster_config.nodes.cache_clear()
        with patch.dict(os.environ, {"K8S_NODES": "node-a,node-b,node-c"}):
            node = cluster_config.node_by_role("second")
        cluster_config.nodes.cache_clear()
        responses = [
            assistant([tool_call("restart_member", {"node": node})]),
            assistant([]),
        ]
        original = agent.LLMClient
        agent.LLMClient = lambda backend: FakeClient(responses)
        cluster_config.nodes.cache_clear()
        try:
            with patch.dict(os.environ, {"K8S_NODES": "node-a,node-b,node-c"}):
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
            cluster_config.nodes.cache_clear()

        # A repair attempted before submit_diagnosis has no approval token, so
        # it must be blocked and logged as a policy violation.
        self.assertFalse(rec["approval_granted"])
        self.assertIsNone(rec["repair"])
        self.assertTrue(any("diagnosis required" in v for v in rec["policy_violations"]))

    def test_prompt_approval_grants_without_showing_token(self):
        responses = [
            assistant([tool_call("submit_diagnosis", {"root_cause": "etcd_member_unreachable"})]),
            assistant([tool_call("restart_member", {"node": "node-a"})]),
            assistant([]),
        ]
        pause = Mock()
        resume = Mock()
        original = agent.LLMClient
        agent.LLMClient = lambda backend: FakeClient(responses)
        cluster_config.nodes.cache_clear()
        try:
            with patch.dict(os.environ, {"K8S_NODES": "node-a,node-b,node-c"}):
                with patch("builtins.input", return_value="y") as prompt:
                    with patch("tools.tool.repairs.restart_member", return_value="restart ok"):
                        with tempfile.TemporaryDirectory() as d:
                            rec = agent.run_scenario(
                                {
                                    "id": "gate-prompt",
                                    "root_cause": "etcd_member_unreachable",
                                    "acceptable_repairs": ["restart_member"],
                                    "forbidden_repairs": [],
                                },
                                FakeBackend(),
                                "gate-prompt",
                                "true",
                                "true",
                                trace_dir=d,
                                approve="prompt",
                                pause_reporter=pause,
                                resume_reporter=resume,
                            )
                            with open(os.path.join(d, "gate-prompt.jsonl"), encoding="utf-8") as handle:
                                trace_text = handle.read()
        finally:
            agent.LLMClient = original
            cluster_config.nodes.cache_clear()

        self.assertTrue(rec["approval_granted"])
        self.assertEqual(rec["repair"], "restart_member")
        self.assertIn("Approve repair", prompt.call_args.args[0])
        self.assertNotIn("approved:", prompt.call_args.args[0])
        self.assertNotIn("approved:gate-prompt", trace_text)
        self.assertNotIn("approved:gate-prompt", json.dumps(rec))
        pause.assert_called_once_with()
        resume.assert_called_once_with()

    def test_prompt_approval_can_be_declined(self):
        responses = [
            assistant([tool_call("submit_diagnosis", {"root_cause": "etcd_member_unreachable"})]),
            assistant([tool_call("restart_member", {"node": "node-a"})]),
            assistant([]),
        ]
        original = agent.LLMClient
        agent.LLMClient = lambda backend: FakeClient(responses)
        cluster_config.nodes.cache_clear()
        try:
            with patch.dict(os.environ, {"K8S_NODES": "node-a,node-b,node-c"}):
                with patch("builtins.input", return_value="n"):
                    with patch("tools.tool.repairs.restart_member", return_value="restart ok") as repair:
                        with tempfile.TemporaryDirectory() as d:
                            rec = agent.run_scenario(
                                {
                                    "id": "gate-decline",
                                    "root_cause": "etcd_member_unreachable",
                                    "acceptable_repairs": ["restart_member"],
                                    "forbidden_repairs": [],
                                },
                                FakeBackend(),
                                "gate-decline",
                                "true",
                                "true",
                                trace_dir=d,
                                approve="prompt",
                            )
        finally:
            agent.LLMClient = original
            cluster_config.nodes.cache_clear()

        self.assertFalse(rec["approval_granted"])
        self.assertIsNone(rec["repair"])
        repair.assert_not_called()
        self.assertTrue(any("diagnosis required first" in v for v in rec["policy_violations"]))

    def test_repair_timeout_result_asks_for_escalation(self):
        responses = [
            assistant([tool_call("submit_diagnosis", {"root_cause": "etcd_member_unreachable"})]),
            assistant([tool_call("restart_member", {"node": "node-a"})]),
            assistant([]),
        ]
        original = agent.LLMClient
        agent.LLMClient = lambda backend: FakeClient(responses)
        cluster_config.nodes.cache_clear()
        try:
            with patch.dict(os.environ, {"K8S_NODES": "node-a,node-b,node-c"}):
                with patch("tools.tool.repairs.restart_member", return_value="timeout waiting for etcd-node-a"):
                    with tempfile.TemporaryDirectory() as d:
                        rec = agent.run_scenario(
                            {
                                "id": "gate-timeout",
                                "root_cause": "etcd_member_unreachable",
                                "acceptable_repairs": ["restart_member"],
                                "forbidden_repairs": [],
                            },
                            FakeBackend(),
                            "gate-timeout",
                            "true",
                            "true",
                            trace_dir=d,
                        )
                        with open(rec["trace"], encoding="utf-8") as fh:
                            trace_text = fh.read()
        finally:
            agent.LLMClient = original
            cluster_config.nodes.cache_clear()

        self.assertEqual(rec["repair"], "restart_member")
        self.assertIn("Repair did not complete", trace_text)
        self.assertIn("escalate to the operator", trace_text)

    def test_prompt_has_blocked_and_timeout_guidance(self):
        prompt = SystemPrompt().render()
        self.assertIn("If a repair call is BLOCKED", prompt)
        self.assertIn("If a repair reports a timeout", prompt)

    def test_system_prompt_uses_optional_topology(self):
        topology = {
            "control_planes": ["vm1", "vm2", "vm3"],
            "probe_node": "vm2",
            "quorum": 2,
            "quorum_loss_targets": 2,
        }
        prompt = SystemPrompt(topology).render()
        self.assertIn("3-control-plane", prompt)
        self.assertIn("The discoverable node names are: vm1, vm2, vm3", prompt)
        self.assertIn("The default probe node is vm2", prompt)
        self.assertIn("Etcd quorum is 2 of 3", prompt)

    def test_repair_ready_timeout_is_configurable(self):
        with patch.dict(os.environ, {"CHAOS_REPAIR_READY_TIMEOUT": "240"}):
            self.assertEqual(repairs.repair_ready_timeout(), 240)
        with patch.dict(os.environ, {"CHAOS_REPAIR_READY_TIMEOUT": "bad"}):
            self.assertEqual(repairs.repair_ready_timeout(), 180)


if __name__ == "__main__":
    unittest.main(verbosity=2)
