#!/usr/bin/env python3
"""Score aggregation tests for authoritative SQLite runs."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import score
from harness.gate_runtime import GateRuntime
from harness.run_runtime import RunRuntime
from harness.tool_runtime import ToolLedger
from storage.sqlite_store import SQLiteStore

ROOT = Path(__file__).resolve().parents[1]


def finished_record(run_id: str, correct: bool) -> dict:
    return {
        "run_id": run_id,
        "scenario": "etcd-member-down",
        "backend": "fake",
        "truth": "etcd_member_unreachable",
        "diagnosis": "etcd_member_unreachable" if correct else "no_fault",
        "diagnosis_correct": correct,
        "approval_granted": correct,
        "fault_present": True,
        "repair": "restart_member" if correct else None,
        "repair_args": {"node": "node-b"} if correct else None,
        "policy_violations": [],
        "forbidden_reason": None,
        "verify_ok": correct,
        "steps": 5,
        "tokens_used": 100,
        "trace": f"traces/{run_id}.jsonl",
    }


class TestScoreDb(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "score.sqlite3"
        self.store = SQLiteStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _seed(self, run_id: str, resume_count: int, interactive: bool = False) -> None:
        self.store.create_run(
            run_id,
            scenario_id="etcd-member-down",
            backend="fake",
            truth="etcd_member_unreachable",
            status="running",
            resume_count=resume_count,
            interactive=interactive,
        )
        log = lambda event: self.store.add_event(run_id, event["type"], event)
        run = RunRuntime(run_id, log=log)
        run.create()
        run.lease()
        run.begin_reset()
        run.reset_ok()
        run.baseline_ok()
        run.inject_ok()
        run.fault_observed()
        gate = GateRuntime(run_id, log=log)
        gate.submit_diagnosis(
            label="etcd_member_unreachable",
            correct=True,
            approved=True,
            attempts=1,
            first_attempt_correct=True,
            source="policy",
        )
        gate.propose_repair(
            "restart_member",
            {"node": "node-b"},
            {"acceptable_repairs": ["restart_member"], "forbidden_repairs": []},
        )
        tool = ToolLedger(run_id, log=log)
        tool_call = tool.for_call("call-1", "restart_member", True, False)
        tool_call.start()
        tool_call.success()
        gate.finish_repair(status="success")
        run.agent_done(gate_terminal="repaired", gate_reason="verify_ok")
        run.verified()
        self.store.add_event(run_id, "assistant", {"tokens": 100})
        self.store.finish_run(
            run_id,
            finished_record(run_id, True),
            status=run.state.status,
            end_reason=run.end_reason,
        )

    def test_load_records_default_excludes_resumed_runs(self) -> None:
        self._seed("clean", 0)
        self._seed("resumed", 1)
        default = self.store.load_records()
        included = self.store.load_records(include_resumed=True)
        self.assertEqual([r["run_id"] for r in default], ["clean"])
        self.assertEqual(len(included), 2)
        self.assertEqual(score.summarize(default)["runs"], 1)
        self.assertEqual(default[0]["scorer_version"], "gate-path-v1")
        self.assertTrue(default[0]["strict_loop_success"])

    def test_score_cli_db_matches_expected_counts(self) -> None:
        self._seed("clean", 0)
        self._seed("resumed", 1)
        cmd = [sys.executable, str(ROOT / "score.py"), "--db", str(self.db_path), "--format", "json"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["overall"]["runs"], 1)
        self.assertEqual(payload["overall"]["diagnosis"], 1.0)
        self.assertEqual(payload["overall"]["strict_loop_success"], 1.0)

        with_resumed = subprocess.run(cmd + ["--include-resumed"], capture_output=True, text=True, check=True)
        payload = json.loads(with_resumed.stdout)
        self.assertEqual(payload["overall"]["runs"], 2)

    def test_db_excludes_interactive_runs_by_default(self) -> None:
        self._seed("clean", 0)
        self._seed("interactive", 0, interactive=True)
        default = self.store.load_records()
        included = self.store.load_records(include_interactive=True)
        self.assertEqual([r["run_id"] for r in default], ["clean"])
        self.assertEqual(len(included), 2)
        cmd = [sys.executable, str(ROOT / "score.py"), "--db", str(self.db_path), "--format", "json"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["overall"]["runs"], 1)
        result = subprocess.run(cmd + ["--include-interactive"], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["overall"]["runs"], 2)

    def test_db_excludes_provider_errors_by_default(self) -> None:
        self._seed("clean", 0)
        self.store.create_run(
            "provider",
            scenario_id="etcd-member-down",
            backend="fake",
            truth="etcd_member_unreachable",
        )
        self.store.finish_run("provider", finished_record("provider", True), status="aborted", end_reason="provider_error_429")
        self.assertEqual([r["run_id"] for r in self.store.load_records()], ["clean"])
        included = self.store.load_records(include_provider_errors=True)
        self.assertIn("provider", [r["run_id"] for r in included])


if __name__ == "__main__":
    unittest.main(verbosity=2)
