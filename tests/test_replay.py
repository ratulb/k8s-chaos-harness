from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from replay import build_html, write_replay
from storage.sqlite_store import SQLiteStore


class TestReplay(unittest.TestCase):
    def _seed(self, db: Path, run_id: str, interactive: bool) -> None:
        store = SQLiteStore(db)
        scenario = {"id": "etcd-member-down", "root_cause": "etcd_member_unreachable"}
        spec_hash = store.hash_spec(scenario)
        version = store.ensure_scenario(scenario["id"], spec_hash, json.dumps(scenario))
        store.create_run(
            run_id,
            scenario_id=scenario["id"],
            scenario_version=version,
            spec_hash=spec_hash,
            backend="ollama",
            truth=scenario["root_cause"],
            status="finished",
            phase="finished",
            topology_hash="topology-1",
            interactive=interactive,
            seed=7,
        )
        store.add_artifact(
            run_id,
            "topology_full",
            json.dumps({"etcd_member_count": 3, "source": "fixture"}),
            "application/json",
        )
        store.add_event(
            run_id,
            "approval",
            {"approval_token": "approved:secret-value", "diagnosis": "etcd_member_unreachable"},
        )
        store.append_ui_chunk(run_id, "text", "approval_token=approved:secret-value <script>bad()</script>")
        store.finish_run(run_id, {
            "run_id": run_id,
            "scenario": "etcd-member-down",
            "backend": "ollama",
            "seed": 7,
            "diagnosis_correct": True,
            "repair": "restart_member",
            "verify_ok": True,
        })
        store.close()

    def test_static_html_redacts_and_builds_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "results.sqlite3"
            self._seed(db, "run-1", interactive=False)
            text = build_html(db)
            self.assertIn("Chaos harness replay", text)
            self.assertIn("run-1", text)
            self.assertIn("n3-fixture", text)
            self.assertIn("&lt;script&gt;", text)
            self.assertNotIn("<script>", text)
            self.assertNotIn("approved:secret-value", text)
            self.assertIn("&lt;redacted&gt;", text)

    def test_interactive_runs_excluded_unless_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "results.sqlite3"
            self._seed(db, "interactive-run", interactive=True)
            default = build_html(db)
            included = build_html(db, include_interactive=True)
            self.assertIn("interactive-run", default)
            self.assertNotIn("n3-fixture", default)
            self.assertIn("n3-fixture", included)

    def test_write_replay_returns_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "results.sqlite3"
            output = root / "reports" / "replay.html"
            self._seed(db, "run-1", interactive=False)
            payload = write_replay(db, output)
            self.assertTrue(output.exists())
            self.assertEqual(payload["output"], str(output))
            self.assertGreater(payload["bytes"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
