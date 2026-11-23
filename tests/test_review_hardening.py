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


class ReviewHardeningTests(unittest.TestCase):
    def _score_record(self, **overrides: object) -> dict:
        record = {
            "truth": "etcd_quorum_loss",
            "diagnosis": "etcd_quorum_loss",
            "diagnosis_correct": True,
            "diagnosis_attempts": 1,
            "diagnosis_first_attempt_correct": True,
            "approval_granted": True,
            "verify_ok": True,
            "repair": "snapshot_restore_full",
            "repair_forbidden": False,
            "policy_violations": [],
            "fault_present": True,
            "end_reason": "submitted",
        }
        record.update(overrides)
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

    def test_journal_redacts_stored_events_but_preserves_approval_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(Path(tmp) / "results.sqlite3")
            store.create_run("journal-redact", scenario_id="control", backend="fake", truth="no_fault")
            journal = Journal("journal-redact", store)
            tool_event = {
                "type": "tool",
                "name": "restart_member",
                "status": "finished",
                "args": {"approval_token": "approved:journal-redact", "node": "vm1"},
                "result": "restarted approved:journal-redact",
            }
            journal.event(tool_event)
            journal.event({
                "type": "approval",
                "granted": True,
                "diagnosis": "no_fault",
                "diagnosis_attempts": 1,
                "diagnosis_correct": True,
                "diagnosis_first_attempt_correct": True,
                "approval_token": "approved:journal-redact",
                "mode": "auto",
                "raw": "diagnosis",
            })
            events = store.load_events("journal-redact")
            rebuilt = store.rebuild_state("journal-redact")
            store.close()
        stored_tool = next(event for event in events if event["type"] == "tool")
        stored_approval = next(event for event in events if event["type"] == "approval")
        self.assertEqual(stored_tool["args"]["approval_token"], "[REDACTED]")
        self.assertNotIn("approved:journal-redact", stored_tool["result"])
        self.assertEqual(stored_approval["approval_token"], "approved:journal-redact")
        self.assertEqual(rebuilt["approval_token"], "approved:journal-redact")
        self.assertIn("approved:journal-redact", tool_event["args"]["approval_token"])

    def test_diagnosis_attempt_cap_withholds_receipt(self) -> None:
        state = AgentState(
            scenario={"root_cause": "etcd_member_unreachable"},
            run_id="cap-run",
            approve="auto",
            diagnosis_attempts=2,
        )
        outcome = Diagnosis(state).execute(
            Tool("submit_diagnosis", "submit"),
            {"root_cause": "etcd_member_unreachable"},
            "call-1",
        )
        self.assertFalse(outcome.diagnosis_correct)
        self.assertIsNone(outcome.approval_token)
        self.assertEqual(outcome.diagnosis_attempts, 3)
        self.assertIn("exhausted", outcome.message)

    def test_repair_alias_is_canonicalized_before_policy_and_artifact(self) -> None:
        state = AgentState(
            scenario={
                "acceptable_repairs": ["full_cluster_reset"],
                "forbidden_repairs": [],
            },
            run_id="alias-run",
            approve="auto",
            diagnosis_submitted=True,
            diagnosis_correct=True,
            diagnosis_attempts=1,
            diagnosis_first_attempt_correct=True,
            approval_token="approved:alias-run",
            approval_source="policy",
        )
        tool = Tool(
            "reset_cluster",
            "legacy alias",
            handler=lambda _args: "reset ok",
            mutating=True,
            destructive=True,
        )
        outcome = Repair(state, registry=None).execute(
            tool,
            {"approval_token": "[REDACTED]"},
            "call-1",
        )
        self.assertEqual(outcome.repair, "full_cluster_reset")
        self.assertEqual(outcome.artifact_payload["tool"], "full_cluster_reset")
        state.apply(outcome)
        self.assertEqual(state.repair, "full_cluster_reset")

    def test_rebuild_state_clears_receipt_after_later_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(Path(tmp) / "results.sqlite3")
            store.create_run("rebuild", scenario_id="s", backend="b", truth="t")
            store.add_event("rebuild", "approval", {
                "granted": True,
                "diagnosis": "etcd_member_unreachable",
                "raw": "etcd member unreachable",
                "diagnosis_attempts": 1,
                "diagnosis_first_attempt_correct": True,
                "approval_token": "approved:rebuild",
            })
            store.add_event("rebuild", "approval", {
                "granted": False,
                "diagnosis": "no_fault",
                "raw": "no fault",
                "diagnosis_attempts": 2,
                "diagnosis_first_attempt_correct": False,
                "approval_token": None,
            })
            state = store.rebuild_state("rebuild")
            self.assertIsNone(state["approval_token"])
            self.assertFalse(state["diagnosis_correct"])
            self.assertTrue(state["diagnosis_first_attempt_correct"])
            self.assertEqual(state["diagnosis_attempts"], 2)
            store.close()

    def test_load_records_preserves_integer_diagnosis_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(Path(tmp) / "results.sqlite3")
            store.create_run("attempts", scenario_id="s", backend="b", truth="etcd_member_unreachable")
            record = self._score_record(repair="restart_member")
            store.finish_run(
                "attempts",
                record,
                status="finished",
                score={
                    "scorer_version": SCORER_VERSION,
                    "diagnosis_attempts": 3,
                    "diagnosis_first_attempt_correct": False,
                },
            )
            loaded = store.load_records()[0]
            self.assertEqual(loaded["diagnosis_attempts"], 3)
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
            store.create_run("rebuild", scenario_id="s", backend="b", truth="t")
            store.add_event("rebuild", "approval", {
                "granted": True,
                "diagnosis": "etcd_member_unreachable",
                "raw": "etcd member unreachable",
                "diagnosis_attempts": 2,
                "diagnosis_first_attempt_correct": False,
                "approval_token": "approved:rebuild",
            })
            state = store.rebuild_state("rebuild")
            self.assertEqual(state["approval_token"], "approved:rebuild")
            self.assertEqual(state["diagnosis_attempts"], 2)
            self.assertFalse(state["diagnosis_first_attempt_correct"])
            store.set_state("rebuild", state)
            restored = store.get_state("rebuild")
            self.assertEqual(restored["approval_token"], "approved:rebuild")
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
