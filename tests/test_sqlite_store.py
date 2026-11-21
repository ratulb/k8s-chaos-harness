#!/usr/bin/env python3
"""Unit tests for the authoritative SQLite event store.

These tests intentionally avoid the network, the live cluster, and the LLM.
They use temporary databases so every assertion is deterministic and can run
on a clean machine. The comments explain both the expected behavior and why
that behavior matters for crash-safe resume.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage.sqlite_store import SQLiteStore


class TestSQLiteStore(unittest.TestCase):
    def setUp(self) -> None:
        # Each test gets an isolated temp directory and a fresh connection.
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "runs.sqlite3"
        self.store = SQLiteStore(self.db_path)

    def tearDown(self) -> None:
        # Close SQLite before the temp directory is removed, otherwise Windows
        # style file locks could leak between tests.
        self.store.close()
        self.tmp.cleanup()

    def _make_run(self, run_id: str = "run-1") -> dict:
        # A scenario must be pinned before a run references it. Editing the spec
        # later should create a new version, not silently mix results.
        spec = {"id": "etcd-member-down", "root_cause": "etcd_member_unreachable"}
        spec_hash = self.store.hash_spec(spec)
        version = self.store.ensure_scenario(spec["id"], spec_hash, json.dumps(spec))
        return self.store.create_run(
            run_id,
            scenario_id=spec["id"],
            scenario_version=version,
            spec_hash=spec_hash,
            backend="fake",
            truth=spec["root_cause"],
            status="running",
            phase="agent_running",
        )

    def test_schema_and_scenario_versioning(self) -> None:
        # init_db() creates all event tables, and ensure_scenario() is
        # idempotent for the same content hash.
        spec = {"id": "etcd-disk-full", "root_cause": "etcd_disk_exhaustion"}
        first_hash = self.store.hash_spec(spec)
        first = self.store.ensure_scenario(spec["id"], first_hash, json.dumps(spec))
        again = self.store.ensure_scenario(spec["id"], first_hash, json.dumps(spec))
        self.assertEqual(first, 1)
        self.assertEqual(again, 1)

        # A changed spec is a new immutable version so old runs stay comparable.
        changed = dict(spec, inject="faults/new.sh")
        second_hash = self.store.hash_spec(changed)
        second = self.store.ensure_scenario(spec["id"], second_hash, json.dumps(changed))
        self.assertEqual(second, 2)

        report = self.store.doctor()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["counts"]["scenarios"], 2)

    def test_append_only_messages_events_and_artifacts(self) -> None:
        self._make_run()
        # Messages and events are append-only. save_messages() appends only the
        # tail, so a resumed run never rewrites earlier conversation turns.
        first_seq = self.store.append_message("run-1", "system", "prompt")
        second_seq = self.store.append_message("run-1", "user", "investigate")
        self.assertEqual((first_seq, second_seq), (1, 2))
        self.store.save_messages(
            "run-1",
            [
                {"role": "system", "content": "prompt"},
                {"role": "user", "content": "investigate"},
                {"role": "assistant", "content": "thinking"},
            ],
        )
        messages = self.store.load_messages("run-1")
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant"])

        # Every event advances runs.seq; that marker is the checkpoint boundary.
        event_seq = self.store.add_event("run-1", "assistant", {"message": {"role": "assistant"}})
        self.assertEqual(event_seq, 1)
        self.assertEqual(self.store.get_run("run-1")["seq"], 1)
        self.assertEqual(self.store.load_events("run-1")[0]["type"], "assistant")

        # Full text is stored in artifacts while JSONL/traces keep truncated text.
        artifact_seq = self.store.add_artifact("run-1", "tool_output_full", "x" * 10000)
        artifact = self.store.get_latest_artifact("run-1", "tool_output_full")
        self.assertEqual(artifact_seq, 1)
        self.assertEqual(len(artifact["content"]), 10000)
        self.assertEqual(artifact["sha256"], hashlib.sha256(("x" * 10000).encode()).hexdigest())

    def test_tool_call_intent_result_lifecycle(self) -> None:
        self._make_run()
        # Intent is written before execution. Until finish_tool_call(), the row
        # exposes an unresolved boundary that resume logic can inspect.
        self.store.begin_tool_call(
            "run-1", "call-1", 2, "member_remove_readd", {"node": "node-b"}, True, True
        )
        self.assertTrue(self.store.has_unknown_mutating_tool_call("run-1"))
        self.store.update_tool_call_status("run-1", "call-1", "started")
        self.store.finish_tool_call(
            "run-1",
            "call-1",
            "finished",
            "raw full result",
            "short result",
            True,
            approved_by="policy",
        )
        self.assertFalse(self.store.has_unknown_mutating_tool_call("run-1"))
        call = self.store.load_tool_calls("run-1")[0]
        self.assertEqual(call["status"], "finished")
        self.assertEqual(call["result_raw"], "raw full result")
        self.assertEqual(call["result_sent"], "short result")
        self.assertEqual(call["approved_by"], "policy")
        self.assertEqual(call["args"], {"node": "node-b"})

    def test_state_cache_is_rebuilt_from_event_log_when_stale(self) -> None:
        self._make_run()
        self.store.append_message("run-1", "system", "prompt")
        self.store.add_event("run-1", "assistant", {"message": {"role": "assistant"}})
        self.store.add_event(
            "run-1",
            "approval",
            {"granted": True, "diagnosis": "etcd_member_unreachable", "raw": "etcd_member_unreachable", "approval_token": "approved:run-1"},
        )
        self.store.add_llm_request("run-1", "fake", 12, 3, 4)

        # Simulate a stale cache: state_seq 999 does not match runs.seq 2.
        # get_state() must derive state from the authoritative log instead.
        self.store.set_state("run-1", {"seq": 999, "diagnosis": "wrong"})
        state = self.store.get_state("run-1")
        self.assertEqual(state["seq"], 2)
        self.assertEqual(state["steps"], 1)
        self.assertEqual(state["tokens_used"], 7)
        self.assertEqual(state["diagnosis"], "etcd_member_unreachable")
        self.assertEqual(state["approval_token"], "approved:run-1")

    def test_finish_run_writes_terminal_fields_score_and_artifact(self) -> None:
        self._make_run()
        record = {
            "run_id": "run-1",
            "scenario": "etcd-member-down",
            "backend": "fake",
            "truth": "etcd_member_unreachable",
            "diagnosis": "etcd_member_unreachable",
            "diagnosis_correct": True,
            "approval_granted": True,
            "verify_ok": True,
            "repair": "restart_member",
            "steps": 3,
            "tokens_used": 42,
        }
        score = {
            "diagnosis_correct": True,
            "repair_action": "restart_member",
            "repair_acceptable": True,
            "repair_forbidden": False,
            "recovered": True,
            "policy_violations": 0,
            "loop_success": True,
        }
        self.store.finish_run("run-1", record, score=score)
        row = self.store.get_run("run-1")
        self.assertEqual(row["status"], "finished")
        self.assertTrue(row["diagnosis_correct"])
        self.assertEqual(row["tokens_used"], 42)
        final_artifact = self.store.get_latest_artifact("run-1", "final_record")
        self.assertEqual(json.loads(final_artifact["content"])["run_id"], "run-1")
        with self.store._connect() as conn:
            saved_score = conn.execute("SELECT * FROM scores WHERE run_id='run-1'").fetchone()
        self.assertEqual(saved_score["recovered"], 1)
        self.assertEqual(saved_score["repair_action"], "restart_member")


if __name__ == "__main__":
    unittest.main(verbosity=2)
