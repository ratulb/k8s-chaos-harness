#!/usr/bin/env python3
"""JSONL run import tests.

These tests prove that import is idempotent, skips malformed/pre-gate records for
headline scoring, and never mutates the source files.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage.sqlite_store import SQLiteStore
from tools.naming import BACKEND_ALIAS_TOKENS


def record(run_id: str, *, pre_gate: bool = False, backend: str = "groq") -> dict:
    rec = {
        "run_id": run_id,
        "scenario": "etcd-member-down",
        "backend": backend,
        "truth": "etcd_member_unreachable",
        "diagnosis": "etcd_member_unreachable",
        "diagnosis_correct": True,
        "fault_present": True,
        "repair": "restart_member",
        "verify_ok": True,
        "steps": 4,
        "tokens_used": 120,
    }
    if not pre_gate:
        # approval_granted is the marker that separates scored gate-era records
        # from earlier exploratory runs.
        rec.update({"approval_granted": True, "policy_violations": [], "forbidden_reason": None})
    return rec


class TestRunImport(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.trace_dir = self.root / "traces"
        self.trace_dir.mkdir()
        self.runs_path = self.root / "runs.jsonl"
        self.store = SQLiteStore(self.root / "runs.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_import_dedupes_and_excludes_exploratory_records(self) -> None:
        good = record("import-good")
        exploratory = record("import-old", pre_gate=True)
        self.runs_path.write_text(
            "\n".join([
                json.dumps(good),
                json.dumps(exploratory),
                "{not json}",
                json.dumps(good),
            ]) + "\n",
            encoding="utf-8",
        )
        (self.trace_dir / "import-good.jsonl").write_text(
            json.dumps({"type": "assistant", "ts": 100.0, "message": {"role": "assistant"}}) + "\n",
            encoding="utf-8",
        )
        source_before = self.runs_path.read_text(encoding="utf-8")

        result = self.store.import_runs(self.runs_path, self.trace_dir)
        self.assertEqual(result["imported"], 2)
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(result["trace_events"], 1)
        self.assertEqual(self.runs_path.read_text(encoding="utf-8"), source_before)

        scored = self.store.load_records()
        self.assertEqual([r["run_id"] for r in scored], ["import-good"])
        with self.store._connect() as conn:
            old = conn.execute("SELECT status, end_reason FROM runs WHERE run_id='import-old'").fetchone()
        self.assertEqual(old["status"], "aborted")
        self.assertEqual(old["end_reason"], "imported_exploratory")

        # A second import is a no-op because run_id is the idempotency key.
        second = self.store.import_runs(self.runs_path, self.trace_dir)
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["skipped"], 4)

    def test_backend_alias_normalizes_to_ollama(self) -> None:
        aliased = record("import-external", backend=BACKEND_ALIAS_TOKENS[0])
        self.runs_path.write_text(json.dumps(aliased) + "\n", encoding="utf-8")
        source_before = self.runs_path.read_text(encoding="utf-8")

        self.store.import_runs(self.runs_path, self.trace_dir)

        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT backend FROM runs WHERE run_id='import-external'"
            ).fetchone()
        self.assertEqual(row["backend"], "ollama")
        self.assertEqual(self.runs_path.read_text(encoding="utf-8"), source_before)

        exported = self.store.load_records()
        self.assertEqual(exported[0]["backend"], "ollama")

    def test_export_regenerates_jsonl_from_database(self) -> None:
        self.runs_path.write_text(json.dumps(record("import-good")) + "\n", encoding="utf-8")
        self.store.import_runs(self.runs_path, self.trace_dir)
        export_path = self.root / "exported.jsonl"
        stats = self.store.export_all(export_path, self.trace_dir)
        self.assertEqual(stats["runs"], 1)
        exported = [json.loads(line) for line in export_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(exported[0]["run_id"], "import-good")


if __name__ == "__main__":
    unittest.main(verbosity=2)
