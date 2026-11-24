from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from harness.journal import Journal
from harness.run_runtime import RunRuntime
from storage.sqlite_store import SQLiteStore


class TestDoctor(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "doctor.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_run(self) -> None:
        store = SQLiteStore(self.db)
        store.create_run(
            "run-1",
            scenario_id="etcd-member-down",
            backend="ollama",
            status="pending",
            phase="queued",
            pid=-1,
        )
        runtime = RunRuntime("run-1", log=Journal("run-1", store).event)
        runtime.create()
        runtime.lease()
        store.close()

    def _doctor(self) -> dict:
        store = SQLiteStore(self.db)
        try:
            return store.doctor()
        finally:
            store.close()

    def test_clean_run_passes_doctor(self) -> None:
        self._seed_run()
        report = self._doctor()
        self.assertTrue(report["ok"], report["issues"])

    def test_terminal_run_without_end_reason_is_reported(self) -> None:
        self._seed_run()
        store = SQLiteStore(self.db)
        with store._connect() as conn:
            conn.execute(
                "UPDATE runs SET status='finished', end_reason=NULL WHERE run_id='run-1'"
            )
        store.close()
        report = self._doctor()
        self.assertIn("run-1", report["terminal_runs_missing_end_reason"])
        self.assertFalse(report["ok"])

    def test_expired_run_with_live_owner_is_reported(self) -> None:
        self._seed_run()
        store = SQLiteStore(self.db)
        with store._connect() as conn:
            conn.execute(
                "UPDATE runs SET lease_expires_at=1.0, pid=? WHERE run_id='run-1'",
                (os.getpid(),),
            )
        store.close()
        report = self._doctor()
        self.assertIn("run-1", report["expired_runs_with_live_owner"])
        self.assertFalse(report["ok"])

    def test_duplicate_generated_keys_are_reported(self) -> None:
        self._seed_run()
        store = SQLiteStore(self.db)
        payload = {"idempotency_key": "reap:run-1:leased:100.0"}
        store.add_event("run-1", "run.lease_expired", payload)
        store.add_event("run-1", "run.lease_expired", payload)
        store.close()
        report = self._doctor()
        self.assertIn(
            "run-1:reap:run-1:leased:100.0",
            report["duplicate_generated_transition_keys"],
        )

    def test_cache_projection_drift_is_reported(self) -> None:
        self._seed_run()
        store = SQLiteStore(self.db)
        row = store.get_run("run-1")
        with store._connect() as conn:
            conn.execute(
                "UPDATE runs SET state_json=?, state_seq=? WHERE run_id='run-1'",
                (
                    json.dumps({"phase": "leased", "approval_granted": True}),
                    row["seq"],
                ),
            )
        store.close()
        report = self._doctor()
        self.assertIn("run-1:approval_granted", report["cache_projection_drift"])
        self.assertNotIn("run-1", report["runs_with_stale_state_cache"])

    def test_agent_end_reason_is_not_cache_projection_drift(self) -> None:
        self._seed_run()
        store = SQLiteStore(self.db)
        row = store.get_run("run-1")
        folded = store.rebuild_state("run-1")
        cache = {
            key: folded.get(key)
            for key in (
                "status", "phase", "fault_present", "verify_ok",
                "diagnosis", "diagnosis_correct", "approval_granted", "repair",
                "forbidden_reason",
            )
        }
        with store._connect() as conn:
            conn.execute(
                "UPDATE runs SET state_json=?, state_seq=?, end_reason='budget_tokens' WHERE run_id='run-1'",
                (json.dumps(cache), row["seq"]),
            )
        store.close()
        report = self._doctor()
        self.assertNotIn("run-1:end_reason", report["cache_projection_drift"])
        self.assertTrue(report["ok"], report["issues"])

    def test_unknown_mutating_tool_effect_is_reported(self) -> None:
        self._seed_run()
        store = SQLiteStore(self.db)
        with store._connect() as conn:
            conn.execute(
                """INSERT INTO tool_calls(run_id, call_id, name, mutating, phase, result_code)
                   VALUES('run-1', 'call-1', 'kubectl', 1, 'unknown_effect', 'unknown_effect')"""
            )
        store.close()
        report = self._doctor()
        self.assertIn("run-1:call-1", report["finished_runs_with_unknown_tool_effect"])
        self.assertFalse(report["ok"])


if __name__ == "__main__":
    unittest.main()
