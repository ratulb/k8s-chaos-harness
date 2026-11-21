from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage.sqlite_store import SQLiteStore


class TestRunBenchmarkTopology(unittest.TestCase):
    def _seed_old_run(self, db: Path) -> None:
        store = SQLiteStore(db)
        scenario = {"id": "etcd-quorum-loss", "root_cause": "quorum_lost"}
        spec_hash = store.hash_spec(scenario)
        version = store.ensure_scenario(scenario["id"], spec_hash, json.dumps(scenario))
        store.create_run(
            "old-run",
            scenario_id=scenario["id"],
            scenario_version=version,
            spec_hash=spec_hash,
            backend="ollama",
            truth=scenario["root_cause"],
            status="running",
            phase="agent_running",
            topology_hash="old-topology",
            pid=-1,
        )
        store.close()

    def _runner(self, db: Path, tmp_path: Path, *extra: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["CHAOS_TOPOLOGY_JSON"] = str(ROOT / "tests" / "fixtures" / "topologies" / "n1.json")
        env["CHAOS_CLUSTER_ID"] = "fixture"
        env["CHAOS_ALLOWED_CLUSTER_IDS"] = "fixture"
        env["CHECKPOINT_DB"] = str(db)
        return subprocess.run(
            [
                sys.executable, str(ROOT / "run_benchmark.py"),
                "--scenario", "etcd-quorum-loss",
                "--backend", "ollama",
                "--db", str(db),
                "--no-jsonl",
                "--runs-file", str(tmp_path / "runs.jsonl"),
                "--trace-dir", str(tmp_path / "traces"),
                *extra,
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_single_member_quorum_loss_is_recorded_as_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db = tmp_path / "results.sqlite3"
            env = os.environ.copy()
            env["CHAOS_TOPOLOGY_JSON"] = str(ROOT / "tests" / "fixtures" / "topologies" / "n1.json")
            env["CHAOS_CLUSTER_ID"] = "fixture"
            env["CHAOS_ALLOWED_CLUSTER_IDS"] = "fixture"
            env["CHECKPOINT_DB"] = str(db)
            result = subprocess.run(
                [
                    sys.executable, str(ROOT / "run_benchmark.py"),
                    "--scenario", "etcd-quorum-loss",
                    "--backend", "ollama",
                    "--db", str(db),
                    "--no-jsonl",
                    "--runs-file", str(tmp_path / "runs.jsonl"),
                    "--trace-dir", str(tmp_path / "traces"),
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(record["end_reason"], "single-member quorum loss is total outage; not scored in benchmark mode")

            conn = sqlite3.connect(db)
            status, end_reason, run_topology = conn.execute(
                "SELECT status, end_reason, topology_hash FROM runs"
            ).fetchone()
            batch_topology = conn.execute("SELECT topology_hash FROM batches").fetchone()[0]
            self.assertEqual(status, "skipped_unsupported")
            self.assertIn("total outage", end_reason)
            self.assertTrue(run_topology)
            self.assertEqual(run_topology, batch_topology)
            kinds = {row[0] for row in conn.execute("SELECT kind FROM run_artifacts")}
            self.assertIn("topology_full", kinds)
            self.assertIn("capability_full", kinds)
            event_types = {row[0] for row in conn.execute("SELECT type FROM trace_events")}
            self.assertIn("topology", event_types)
            self.assertIn("capability", event_types)
            conn.close()

    def test_topology_mismatch_auto_discards_stale_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db = tmp_path / "results.sqlite3"
            self._seed_old_run(db)

            result = self._runner(db, tmp_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("discarding stale state", result.stdout)
            conn = sqlite3.connect(db)
            rows = conn.execute("SELECT run_id, topology_hash FROM runs").fetchall()
            batches = conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
            conn.close()
            self.assertEqual(len(rows), 1)
            self.assertNotEqual(rows[0][0], "old-run")
            self.assertNotEqual(rows[0][1], "old-topology")
            self.assertEqual(batches, 1)

    def test_explicit_resume_refuses_topology_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db = tmp_path / "results.sqlite3"
            self._seed_old_run(db)

            result = self._runner(db, tmp_path, "--resume-of", "old-run")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing resume", result.stderr)
            conn = sqlite3.connect(db)
            rows = conn.execute("SELECT run_id, topology_hash FROM runs").fetchall()
            conn.close()
            self.assertEqual(rows, [("old-run", "old-topology")])

    def test_prompt_approval_marks_run_interactive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db = tmp_path / "results.sqlite3"
            result = self._runner(db, tmp_path, "--approve", "prompt")
            self.assertEqual(result.returncode, 0, result.stderr)
            conn = sqlite3.connect(db)
            interactive = conn.execute("SELECT interactive FROM runs").fetchone()[0]
            conn.close()
            self.assertEqual(interactive, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
