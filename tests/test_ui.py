from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.reporter import UiReporter, redact
from harness.ui import LiveView
from run_benchmark import _run
from storage.sqlite_store import SQLiteStore


class TestUi(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "ui.sqlite3"
        self.store = SQLiteStore(self.db_path)
        scenario = {"id": "etcd-member-down", "root_cause": "etcd_member_unreachable"}
        spec_hash = self.store.hash_spec(scenario)
        version = self.store.ensure_scenario(scenario["id"], spec_hash, json.dumps(scenario))
        self.store.create_run(
            "run-1",
            scenario_id=scenario["id"],
            scenario_version=version,
            spec_hash=spec_hash,
            backend="ollama",
            truth=scenario["root_cause"],
            status="running",
            phase="agent_running",
            topology_hash="topology-1",
            started_at=self.store._now() - 12,
            updated_at=self.store._now(),
        )

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_reporter_redacts_secrets_and_writes_ui_stream(self) -> None:
        reporter = UiReporter("run-1", self.store)
        seq = reporter.emit("assistant_delta", "approval_token=approved:run-1 Bearer abc123")
        self.assertGreater(seq, 0)
        chunk = self.store.load_ui_stream("run-1")[0]
        self.assertNotIn("approved:run-1", chunk["text"])
        self.assertNotIn("abc123", chunk["text"])
        self.assertIn("<redacted>", chunk["text"])
        self.assertNotIn("approved:run-1", redact("approval_token: approved:run-1"))
        self.assertNotIn("approved:run-1", redact('{"approval_token": "approved:run-1"}'))

    def test_child_command_output_is_logged(self) -> None:
        log_path = Path(self.tmp.name) / "logs" / "run-1.log"
        ok, output = _run("printf logged-output", 5, None, str(log_path))
        self.assertTrue(ok)
        self.assertEqual(output, "logged-output")
        logged = log_path.read_text(encoding="utf-8")
        self.assertIn("printf logged-output", logged)
        self.assertIn("logged-output", logged)

    def test_live_view_renders_sqlite_state_without_ansi_in_plain_mode(self) -> None:
        self.store.add_event("run-1", "tool", {"name": "etcdctl_member_list", "status": "finished", "result": "ok"})
        self.store.append_ui_chunk("run-1", "assistant_delta", "checking members")
        topology = json.loads(
            (ROOT / "tests" / "fixtures" / "topologies" / "n4.json").read_text(encoding="utf-8")
        )
        self.store.add_artifact("run-1", "topology_full", json.dumps(topology, sort_keys=True), "application/json")
        view = LiveView(self.db_path, "run-1", mode="benchmark")
        text = view.render_text()
        self.assertIn("run-1", text)
        self.assertIn("agent_running", text)
        self.assertIn("checking members", text)
        self.assertIn("etcdctl_member_list", text)
        self.assertIn("cluster:", text)
        output = io.StringIO()
        view.stream = output
        view.render_once()
        self.assertNotIn("\x1b[", output.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
