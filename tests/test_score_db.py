#!/usr/bin/env python3
"""Score aggregation tests for SQLite and JSONL inputs.

Headline rates must exclude resumed runs because they contain an intervention.
The DB path and legacy path should otherwise agree on ordinary finished runs.
"""
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
        self.store.finish_run(run_id, finished_record(run_id, True))

    def test_load_records_default_excludes_resumed_runs(self) -> None:
        # One clean run and one resumed run: only the clean run belongs in the
        # headline table. --include-resumed is an explicit opt-in.
        self._seed("clean", 0)
        self._seed("resumed", 1)
        default = self.store.load_records()
        included = self.store.load_records(include_resumed=True)
        self.assertEqual([r["run_id"] for r in default], ["clean"])
        self.assertEqual(len(included), 2)
        self.assertEqual(score.summarize(default)["runs"], 1)

    def test_score_cli_db_matches_expected_counts(self) -> None:
        self._seed("clean", 0)
        self._seed("resumed", 1)
        cmd = [sys.executable, str(ROOT / "score.py"), "--db", str(self.db_path), "--format", "json"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["overall"]["runs"], 1)
        self.assertEqual(payload["overall"]["diagnosis"], 1.0)

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

    def test_legacy_loader_filters_interactive_and_unsupported(self) -> None:
        runs_path = Path(self.tmp.name) / "runs.jsonl"
        lines = [
            finished_record("plain", True),
            dict(finished_record("interactive", True), interactive=True),
            dict(finished_record("unsupported", True), status="skipped_unsupported", end_reason="unsupported_topology"),
        ]
        runs_path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        default = score.load(str(runs_path))
        included = score.load(str(runs_path), include_interactive=True)
        self.assertEqual([r["run_id"] for r in default], ["plain"])
        self.assertEqual([r["run_id"] for r in included], ["plain", "interactive"])

    def test_legacy_loader_also_filters_resume_count(self) -> None:
        runs_path = Path(self.tmp.name) / "runs.jsonl"
        lines = [finished_record("plain", True), dict(finished_record("retry", True), resume_count=1)]
        runs_path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        self.assertEqual(len(score.load(str(runs_path))), 1)
        self.assertEqual(len(score.load(str(runs_path), include_resumed=True)), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
