#!/usr/bin/env python3
"""Tests for the DB maintenance CLI and shell wrapper.

These are offline smoke tests: they exercise argument parsing and JSON output
without starting a backend or touching the cluster. bash -n validates the
wrapper syntax even when Bash is available but no live infrastructure is.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage.sqlite_store import SQLiteStore


class TestDbCli(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "cli.sqlite3"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _cli(self, *args: str) -> subprocess.CompletedProcess:
        # --db must precede the subcommand; the CLI uses a global argument.
        return subprocess.run(
            [sys.executable, str(ROOT / "db_cli.py"), "--db", str(self.db_path), *args],
            capture_output=True,
            text=True,
            check=True,
        )

    def test_init_migrate_doctor_status_and_export(self) -> None:
        init = json.loads(self._cli("init").stdout)
        self.assertTrue(init["initialized"])
        self.assertTrue(self.db_path.exists())

        migrate = json.loads(self._cli("migrate").stdout)
        self.assertEqual(migrate["schema_version"], 6)

        doctor = json.loads(self._cli("doctor", "--strict").stdout)
        self.assertTrue(doctor["ok"], doctor["issues"])
        self.assertEqual(doctor["schema_version"], 6)
        self.assertIn("state_snapshots", doctor["counts"])
        self.assertIn("ui_stream", doctor["counts"])

        status = json.loads(self._cli("status", "--limit", "2").stdout)
        self.assertEqual(status["counts"]["runs"], 0)
        self.assertEqual(status["recent_runs"], [])

        store = SQLiteStore(self.db_path)
        store.create_run(
            "cli-run",
            scenario_id="etcd-member-down",
            backend="ollama",
            topology_hash="topology-1",
            launch_id="launch-1",
            pid=1234,
            interactive=True,
        )
        store.close()
        status = json.loads(self._cli("status", "--limit", "2").stdout)
        recent = status["recent_runs"][0]
        self.assertEqual(recent["topology_hash"], "topology-1")
        self.assertEqual(recent["launch_id"], "launch-1")
        self.assertEqual(recent["pid"], 1234)
        self.assertEqual(recent["interactive"], 1)

        export = json.loads(self._cli(
            "export",
            "--runs", str(Path(self.tmp.name) / "runs.jsonl"),
            "--trace-dir", str(Path(self.tmp.name) / "traces"),
        ).stdout)
        self.assertEqual(export["runs"], 0)
        self.assertEqual(export["traces"], 0)
        self.assertEqual(export["snapshots"], 0)
        self.assertEqual(export["stream_chunks"], 0)

    def test_wrapper_has_valid_bash_syntax(self) -> None:
        if not shutil.which("bash"):
            self.skipTest("bash not installed")
        wrapper = ROOT / "run_benchmark.sh"
        self.assertTrue(wrapper.exists())
        result = subprocess.run(["bash", "-n", str(wrapper)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("import-legacy", wrapper.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
