#!/usr/bin/env python3
"""Offline crash-resume tests for the agent loop.

A fake LLM supplies deterministic tool calls and a fake repair runner prevents
any cluster mutation. The point is to prove that the SQLite log carries enough
state to continue a diagnosis and that mutating side effects are treated
conservatively.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import harness.agent as agent
import run_benchmark
from harness.gate_runtime import GateRuntime
from storage.sqlite_store import SQLiteStore
from tools import cluster_config


class FakeBackend:
    """The agent only reads backend.name when building records."""
    name = "fake"


class FakeClient:
    """Return scripted chat responses without touching a real LLM."""
    def __init__(self, responses):
        self.responses = list(responses)

    def chat(self, messages, **kwargs):
        return self.responses.pop(0)


def assistant(tool_calls):
    return {
        "choices": [{"message": {"role": "assistant", "content": "", "tool_calls": tool_calls}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }


def tool_call(call_id, name, args):
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(args)}}


class TestResume(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self.tmp.name) / "resume.sqlite3")
        cluster_config.nodes.cache_clear()

    def tearDown(self) -> None:
        self.store.close()
        cluster_config.nodes.cache_clear()
        self.tmp.cleanup()

    def _seed_agent_run(self) -> dict:
        # Simulate a crash after a correct diagnosis opened the gate but before
        # a repair was requested. The canonical gate event log is the source of
        # truth on resume; no receipt is carried in the tool call.
        spec = {"id": "etcd-member-down", "root_cause": "etcd_member_unreachable"}
        spec_hash = self.store.hash_spec(spec)
        version = self.store.ensure_scenario(spec["id"], spec_hash, json.dumps(spec))
        row = self.store.create_run(
            "resume-1",
            scenario_id=spec["id"],
            scenario_version=version,
            spec_hash=spec_hash,
            backend="fake",
            truth=spec["root_cause"],
            status="running",
            phase="agent_running",
        )
        self.store.append_message("resume-1", "system", "prompt")
        self.store.append_message("resume-1", "user", "investigate")
        self.store.add_event("resume-1", "assistant", {"message": {"role": "assistant"}})
        logged: list[dict] = []
        gate = GateRuntime("resume-1", log=logged.append)
        gate.submit_diagnosis(
            label="etcd_member_unreachable",
            correct=True,
            approved=True,
            attempts=1,
            first_attempt_correct=True,
            is_no_fault=False,
            source="policy",
            raw="etcd_member_unreachable",
        )
        seq = 0
        for event in logged:
            seq = self.store.add_event("resume-1", event["type"], event)
        self.store.set_state(
            "resume-1",
            {
                "seq": seq,
                "gate": gate.to_snapshot(),
                "steps": 1,
                "tokens_used": 7,
                "diagnosis_nudged": False,
            },
        )
        return row

    def test_resume_reloads_diagnosis_and_finishes_repair(self) -> None:
        self._seed_agent_run()
        responses = [
            assistant([tool_call("repair-1", "restart_member", {"node": "node-b"})]),
            assistant([]),
        ]
        scenario = {
            "id": "etcd-member-down",
            "root_cause": "etcd_member_unreachable",
            "acceptable_repairs": ["restart_member"],
            "forbidden_repairs": [],
        }

        with patch.dict(os.environ, {"K8S_NODES": "node-a,node-b,node-c"}):
            cluster_config.nodes.cache_clear()
            with patch.object(agent, "LLMClient", lambda backend: FakeClient(responses)), \
                 patch("tools.tool.repairs.restart_member", return_value="restarted node-b"):
                record = agent.run_scenario(
                    scenario,
                    FakeBackend(),
                    "resume-1",
                    "",
                    "true",
                    trace_dir=self.tmp.name,
                    store=self.store,
                    resume=True,
                )
            cluster_config.nodes.cache_clear()

        # The resumed run should reuse the accepted diagnosis and execute the
        # allowed repair exactly once, then terminate normally.
        self.assertTrue(record["approval_granted"])
        self.assertEqual(record["repair"], "restart_member")
        self.assertTrue(record["verify_ok"])
        row = self.store.get_run("resume-1")
        self.assertEqual(row["status"], "finished")
        calls = self.store.load_tool_calls("resume-1")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["status"], "finished")
        self.assertEqual(calls[0]["mutating"], 1)
        self.assertEqual(calls[0]["result_raw"], "restarted node-b")

    def test_unknown_mutating_call_forces_fresh_retry(self) -> None:
        row = self._seed_agent_run()
        # A started mutating call with no result is precisely the ambiguous
        # boundary that must not be replayed in place.
        self.store.begin_tool_call(
            "resume-1", "repair-in-flight", 3, "member_remove_readd", {"node": "node-b"}, True, True
        )
        self.store.update_tool_call_status("resume-1", "repair-in-flight", "started")
        self.assertTrue(self.store.has_unknown_mutating_tool_call("resume-1"))

        retry = run_benchmark._abort_and_retry(
            self.store, row, "unknown_repair_side_effect", "config-hash", row["spec_hash"], row["scenario_version"]
        )
        self.assertEqual(self.store.get_run("resume-1")["status"], "aborted")
        self.assertEqual(retry["resume_of"], "resume-1")
        self.assertEqual(retry["resume_count"], 1)
        self.assertEqual(retry["status"], "pending")


if __name__ == "__main__":
    unittest.main(verbosity=2)
