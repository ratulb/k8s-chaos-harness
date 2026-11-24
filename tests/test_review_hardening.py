from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.scoring import SCORER_VERSION, Score
from harness.journal import Journal
from harness.message import Message
from harness.tooling import AgentState, Diagnosis, Repair
from llm.client import Backend, LLMClient
from storage.sqlite_store import SQLiteStore
from tools.scenario import Scenario
from tools.tool import Tool


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def gate_diagnosis_event(
    *,
    label: str,
    correct: bool,
    approved: bool,
    attempts: int,
    first_attempt_correct: bool,
    raw: str,
) -> dict:
    return {
        "type": "gate.diagnosis_submitted",
        "payload": {
            "event": "diagnosis_submitted",
            "payload": {
                "label": label,
                "correct": correct,
                "approved": approved,
                "attempts": attempts,
                "first_attempt_correct": first_attempt_correct,
                "is_no_fault": label == "no_fault",
                "source": "policy" if approved else "operator",
                "raw": raw,
            },
        },
    }


class ReviewHardeningTests(unittest.TestCase):
    def _score_record(self, **overrides: object) -> dict:
        record = {
            "truth": "etcd_quorum_loss",
            "diagnosis": "etcd_quorum_loss",
            "diagnosis_correct": True,
            "diagnosis_attempts": 1,
            "approval_granted": True,
            "verify_ok": True,
            "repair": "snapshot_restore_full",
            "fault_present": True,
            "end_reason": "verify_ok",
        }
        record.update(overrides)
        record["gate_path"] = {
            "visited": ["await_diagnosis", "approved", "repaired"],
            "terminal": "repaired",
            "terminal_reason": None,
            "diagnosis_submitted": True,
            "first_attempt_correct": bool(record.get("diagnosis_first_attempt_correct", True)),
            "receipt_issued": bool(record["approval_granted"]),
            "repair_proposed": True,
            "repair_executed": True,
            "repair_status": "success",
            "violations": [],
        }
        record["run"] = {
            "state": {
                "status": "finished",
                "phase": "finished",
                "end_reason": record["end_reason"],
                "fault_present": bool(record["fault_present"]),
                "verify_ok": bool(record["verify_ok"]),
            },
        }
        record["tool_states"] = {}
        return record

    def test_score_canonicalizes_alias_and_uses_current_version(self) -> None:
        score = Score.from_record(self._score_record())
        self.assertEqual(score.scorer_version, SCORER_VERSION)
        self.assertEqual(score.repair_action, "full_cluster_reset")
        self.assertTrue(score.strict_loop_success)
        self.assertTrue(score.diagnosis_first_attempt_correct)

    def test_score_rejects_budget_cutoff_and_second_attempt(self) -> None:
        budget = Score.from_record(self._score_record(end_reason="budget_steps"))
        second = Score.from_record(self._score_record(
            diagnosis_attempts=2,
            diagnosis_first_attempt_correct=False,
        ))
        self.assertFalse(budget.strict_loop_success)
        self.assertFalse(second.strict_loop_success)
        self.assertTrue(budget.loop_success)
        self.assertTrue(second.loop_success)

    def test_message_redaction_handles_nested_payloads(self) -> None:
        payload = {
            "args": {"approval_token": "approved:run-1", "node": "vm1"},
            "content": "diagnosis accepted; approval_token=approved:run-1",
        }
        redacted = Message.redact_payload(payload)
        self.assertEqual(redacted["args"]["approval_token"], "[REDACTED]")
        self.assertEqual(redacted["args"]["node"], "vm1")
        self.assertNotIn("approved:run-1", redacted["content"])

    def test_journal_stores_canonical_gate_events_and_rebuilds_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(Path(tmp) / "results.sqlite3")
            store.create_run("journal-gate", scenario_id="control", backend="fake", truth="no_fault")
            journal = Journal("journal-gate", store)
            journal.event({
                "type": "tool",
                "name": "restart_member",
                "status": "finished",
                "args": {"node": "vm1"},
                "result": "restarted",
            })
            journal.event(gate_diagnosis_event(
                label="no_fault",
                correct=True,
                approved=True,
                attempts=1,
                first_attempt_correct=True,
                raw="diagnosis",
            ))
            events = store.load_events("journal-gate")
            rebuilt = store.rebuild_state("journal-gate")
            store.close()
        self.assertTrue(any(event["type"] == "gate.diagnosis_submitted" for event in events))
        self.assertTrue(rebuilt["approval_granted"])
        self.assertEqual(rebuilt["receipt"], "approved:journal-gate")
        self.assertEqual(rebuilt["diagnosis"], "no_fault")

    def test_diagnosis_attempt_cap_withholds_receipt(self) -> None:
        state = AgentState(
            scenario={"root_cause": "etcd_member_unreachable"},
            run_id="cap-run",
            approve="policy",
        )
        tool = Tool("submit_diagnosis", "submit", kind="diagnosis")
        first = Diagnosis(state).execute(tool, {"root_cause": "no_fault"}, "call-1")
        second = Diagnosis(state).execute(tool, {"root_cause": "no_fault"}, "call-2")
        outcome = Diagnosis(state).execute(
            tool,
            {"root_cause": "etcd_member_unreachable"},
            "call-3",
        )
        self.assertTrue(first.diagnosis_submitted)
        self.assertTrue(second.diagnosis_submitted)
        self.assertFalse(outcome.diagnosis_correct)
        self.assertIsNone(state.receipt)
        self.assertEqual(state.diagnosis_attempts, 2)
        self.assertIn("exhausted", outcome.message)

    def test_repair_alias_is_canonicalized_before_policy_and_artifact(self) -> None:
        state = AgentState(
            scenario={
                "root_cause": "etcd_member_unreachable",
                "acceptable_repairs": ["full_cluster_reset"],
                "forbidden_repairs": [],
            },
            run_id="alias-run",
            approve="policy",
        )
        Diagnosis(state).execute(
            Tool("submit_diagnosis", "", kind="diagnosis"),
            {"root_cause": "etcd_member_unreachable"},
            "call-0",
        )
        tool = Tool(
            "reset_cluster",
            "reset alias",
            handler=lambda _args: "reset ok",
            kind="repair",
            mutating=True,
            destructive=True,
        )
        outcome = Repair(state, registry=None).execute(tool, {}, "call-1")
        self.assertEqual(outcome.repair, "full_cluster_reset")
        self.assertEqual(outcome.artifact_payload["tool"], "full_cluster_reset")
        self.assertEqual(state.repair, "full_cluster_reset")

    def test_rebuild_state_clears_receipt_after_later_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(Path(tmp) / "results.sqlite3")
            store.create_run("rebuild", scenario_id="s", backend="b", truth="etcd_member_unreachable")
            store.add_event("rebuild", "gate.diagnosis_submitted", gate_diagnosis_event(
                label="etcd_member_unreachable",
                correct=True,
                approved=True,
                attempts=1,
                first_attempt_correct=True,
                raw="etcd member unreachable",
            ))
            store.add_event("rebuild", "gate.diagnosis_submitted", gate_diagnosis_event(
                label="no_fault",
                correct=False,
                approved=False,
                attempts=2,
                first_attempt_correct=True,
                raw="no fault",
            ))
            state = store.rebuild_state("rebuild")
            self.assertFalse(state["approval_granted"])
            self.assertIsNone(state["receipt"])
            self.assertFalse(state["diagnosis_correct"])
            self.assertTrue(state["diagnosis_first_attempt_correct"])
            self.assertEqual(state["diagnosis_attempts"], 2)
            store.close()

    def test_load_records_folds_integer_diagnosis_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(Path(tmp) / "results.sqlite3")
            store.create_run("attempts", scenario_id="s", backend="b", truth="etcd_member_unreachable")
            for attempts in (1, 2):
                store.add_event("attempts", "gate.diagnosis_submitted", gate_diagnosis_event(
                    label="no_fault",
                    correct=False,
                    approved=False,
                    attempts=attempts,
                    first_attempt_correct=False,
                    raw="no fault",
                ))
            record = self._score_record(repair="restart_member")
            store.finish_run("attempts", record, status="finished")
            loaded = store.load_records()[0]
            self.assertEqual(loaded["diagnosis_attempts"], 2)
            self.assertFalse(loaded["diagnosis_first_attempt_correct"])
            store.close()

    def test_export_all_redacts_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteStore(root / "results.sqlite3")
            store.create_run("export", scenario_id="s", backend="b", truth="t")
            store.add_event("export", "approval", {
                "granted": True,
                "approval_token": "approved:export",
                "diagnosis": "no_fault",
            })
            store.export_all(root / "runs.jsonl", root / "traces")
            trace_text = (root / "traces" / "export.jsonl").read_text()
            self.assertNotIn("approved:export", trace_text)
            store.close()

    def test_rebuild_state_reads_receipt_and_attempt_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(Path(tmp) / "results.sqlite3")
            store.create_run("rebuild", scenario_id="s", backend="b", truth="etcd_member_unreachable")
            store.add_event("rebuild", "gate.diagnosis_submitted", gate_diagnosis_event(
                label="no_fault",
                correct=False,
                approved=False,
                attempts=1,
                first_attempt_correct=False,
                raw="no fault",
            ))
            store.add_event("rebuild", "gate.diagnosis_submitted", gate_diagnosis_event(
                label="etcd_member_unreachable",
                correct=True,
                approved=True,
                attempts=2,
                first_attempt_correct=False,
                raw="etcd member unreachable",
            ))
            state = store.rebuild_state("rebuild")
            self.assertEqual(state["receipt"], "approved:rebuild")
            self.assertEqual(state["diagnosis_attempts"], 2)
            self.assertFalse(state["diagnosis_first_attempt_correct"])
            store.set_state("rebuild", state)
            restored = store.get_state("rebuild")
            self.assertEqual(restored["receipt"], "approved:rebuild")
            with sqlite3.connect(db := Path(tmp) / "results.sqlite3") as conn:
                snapshots = conn.execute("SELECT state_json FROM state_snapshots").fetchall()
            self.assertTrue(snapshots)
            self.assertTrue(all("approved:rebuild" not in row[0] for row in snapshots))
            store.close()

    def test_replay_and_engine_share_score_definition(self) -> None:
        import replay

        rec = self._score_record()
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(Path(tmp) / "results.sqlite3")
            rows = replay._matrix_rows(
                store,
                [{"run_id": "r", "scenario": "s", "backend": "b", **rec}],
            )
            store.close()
        self.assertEqual(rows[0]["strict_loop_success"], 1.0)
        self.assertEqual(rows[0]["first_attempt_diagnosis_rate"], 1.0)

    def test_streaming_fallback_is_visible(self) -> None:
        client = LLMClient(Backend("fake", "http://localhost", "model"))
        body = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": None}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()
        with patch.object(client, "_open_completion", return_value=_FakeResponse(body)):
            response = client.chat([{"role": "user", "content": "hi"}], on_chunk=lambda _text: None)
        self.assertTrue(response["stream_fallback"])
        self.assertEqual(response["choices"][0]["finish_reason"], "reconstructed")

    def test_scenario_validation_rejects_alias_policy_divergence(self) -> None:
        path = ROOT / "scenarios" / "etcd-quorum-loss.json"
        raw = Scenario.from_file(path).raw
        raw["acceptable_repairs"] = ["full_cluster_reset"]
        raw["forbidden_repairs"] = ["snapshot_restore_full"]
        errors = Scenario.from_dict(raw).validate(ROOT, expected_id=raw["id"]).to_list()
        self.assertTrue(any("alias/target policy divergence" in error for error in errors))

    def test_active_run_rows_warning(self) -> None:
        import chaos_cli

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "results.sqlite3"
            store = SQLiteStore(db)
            store.create_run("stuck", scenario_id="s", backend="b", truth="t", status="running")
            store.close()
            warnings = chaos_cli._stale_warnings(str(db), "topology")
            self.assertTrue(any("active run row" in warning for warning in warnings))
            conn = sqlite3.connect(db)
            status = conn.execute("SELECT status FROM runs WHERE run_id='stuck'").fetchone()[0]
            conn.close()
            self.assertEqual(status, "running")


if __name__ == "__main__":
    unittest.main(verbosity=2)
