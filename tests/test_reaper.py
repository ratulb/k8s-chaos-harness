from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from harness.journal import Journal
from harness.reaper import abandon_run, reap_expired
from harness.run_runtime import RunRuntime
from storage.sqlite_store import SQLiteStore, read_only_rows


class TestReaper(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "reaper.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_run(self, pid: int = 999999) -> None:
        store = SQLiteStore(self.db)
        store.create_run(
            "run-1",
            scenario_id="etcd-member-down",
            backend="ollama",
            status="pending",
            phase="queued",
            pid=pid,
        )
        runtime = RunRuntime(
            "run-1", log=Journal("run-1", store).event
        )
        runtime.create()
        runtime.lease()
        with store._connect() as conn:
            conn.execute(
                "UPDATE runs SET lease_expires_at=? WHERE run_id=?",
                (100.0, "run-1"),
            )
        store.close()

    def _events(self, event_type: str) -> list[dict[str, Any]]:
        rows = read_only_rows(
            self.db,
            "SELECT * FROM trace_events WHERE type=? ORDER BY seq",
            (event_type,),
        )
        result = []
        for row in rows:
            payload = row.get("payload_json")
            if isinstance(payload, str):
                payload = json.loads(payload)
            result.append(payload or {})
        return result

    def test_reap_emits_lease_expired_once_with_design_key(self) -> None:
        self._seed_run()
        first = reap_expired(self.db, now=200.0)
        self.assertEqual(first["observed"], ["run-1"])
        events = self._events("run.lease_expired")
        self.assertEqual(len(events), 1)
        event = events[0]
        envelope = event["payload"]
        payload = envelope["payload"]
        self.assertEqual(envelope["idempotency_key"], "reap:run-1:leased:100.0")
        self.assertEqual(payload["previous_state"], "leased")
        self.assertEqual(payload["reason"], "lease_expired")
        self.assertEqual(payload["detected_by"], "reaper")

        second = reap_expired(self.db, now=200.0)
        self.assertEqual(second["observed"], [])
        self.assertEqual(len(self._events("run.lease_expired")), 1)
        store = SQLiteStore(self.db)
        try:
            self.assertEqual(store.get_run("run-1")["phase"], "interrupted")
            self.assertIsNone(store.get_run("run-1")["lease_expires_at"])
        finally:
            store.close()

    def test_reap_skips_live_owner(self) -> None:
        self._seed_run(pid=os.getpid())
        result = reap_expired(self.db, now=200.0)
        self.assertEqual(result["observed"], [])
        self.assertEqual(self._events("run.lease_expired"), [])

    def test_abandon_moves_interrupted_run_to_aborted(self) -> None:
        self._seed_run()
        reap_expired(self.db, now=200.0)
        result = abandon_run(self.db, "run-1")
        self.assertEqual(result["status"], "aborted")
        self.assertEqual(result["end_reason"], "abandoned")
        events = self._events("run.abandon")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["payload"]["actor"], "operator")
        store = SQLiteStore(self.db)
        try:
            self.assertEqual(store.get_run("run-1")["status"], "aborted")
            self.assertEqual(store.get_run("run-1")["end_reason"], "abandoned")
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
