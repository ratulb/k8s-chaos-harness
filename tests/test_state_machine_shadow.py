from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.machines.adapters import (
    classify_repair_result,
    shadow_replay,
)
from harness.machines.gate import GatePhase
from harness.machines.run import RunPhase
from harness.machines.tools import ToolPhase, ToolResultCode
from scripts.gen_state_diagrams import build_document

SCENARIO = {
    "root_cause": "etcd_config_error",
    "acceptable_repairs": ["restart_member"],
    "forbidden_repairs": [],
}


def _run(**changes: object) -> dict[str, object]:
    run = {
        "run_id": "run-1",
        "status": "finished",
        "phase": "finished",
        "end_reason": "verify_ok",
        "backend": "primary",
        "model": "model-1",
        "truth": "etcd_config_error",
        "diagnosis": "etcd_config_error",
        "diagnosis_correct": 1,
        "diagnosis_attempts": 1,
        "diagnosis_first_attempt_correct": 1,
        "approval_granted": 1,
        "repair": "restart_member",
        "verify_ok": 1,
        "fault_present": 1,
        "reset_ok": 1,
        "baseline_ok": 1,
        "inject_ok": 1,
    }
    run.update(changes)
    return run


def _setup_events() -> list[dict[str, object]]:
    return [
        {"seq": 1, "type": "reset", "ok": True},
        {"seq": 2, "type": "baseline", "ok": True},
        {"seq": 3, "type": "inject", "ok": True},
        {"seq": 4, "type": "fault_check", "present": True},
    ]


def _approval(**changes: object) -> dict[str, object]:
    event = {
        "seq": 5,
        "type": "approval",
        "granted": True,
        "diagnosis": "etcd_config_error",
        "diagnosis_correct": True,
        "diagnosis_attempts": 1,
        "diagnosis_first_attempt_correct": True,
        "mode": "policy",
    }
    event.update(changes)
    return event


def _repair_call(**changes: object) -> dict[str, object]:
    call = {
        "call_id": "call-1",
        "name": "restart_member",
        "status": "finished",
        "mutating": 1,
        "destructive": 0,
        "args": {},
        "result_sent": "OK",
        "result_raw": "OK",
        "error": None,
    }
    call.update(changes)
    return call


class LegacyAdapterTests(unittest.TestCase):
    def test_legacy_happy_path_replays_to_matching_machines(self) -> None:
        events = _setup_events()
        events.extend(
            [
                _approval(),
                {
                    "seq": 6,
                    "type": "tool",
                    "name": "restart_member",
                    "status": "finished",
                    "mutating": True,
                    "args": {},
                    "result": "OK",
                },
                {"seq": 7, "type": "verify_started"},
                {"seq": 8, "type": "verify", "ok": True},
            ]
        )
        report = shadow_replay(_run(), events, [_repair_call()], scenario=SCENARIO)

        self.assertTrue(report.ok, report.mismatches)
        self.assertIs(report.gate.phase, GatePhase.REPAIRED)
        self.assertIs(report.run.phase, RunPhase.FINISHED)
        self.assertEqual(report.run.end_reason, "verify_ok")
        self.assertIs(report.tools["call-1"].phase, ToolPhase.SUCCEEDED)
        self.assertEqual(report.tools["call-1"].result_code, ToolResultCode.SUCCESS.value)

    def test_redacted_receipt_and_fallback_provenance_preserve_chain(self) -> None:
        events = _setup_events()
        events.extend(
            [
                {
                    "seq": 5,
                    "type": "assistant",
                    "backend": "secondary",
                    "requested_backend": "primary",
                    "fallback_from": ["primary"],
                    "skipped_backends": [],
                },
                _approval(approval_token="[REDACTED]"),
                {
                    "seq": 6,
                    "type": "tool",
                    "name": "restart_member",
                    "status": "finished",
                    "mutating": True,
                    "args": {},
                    "result": "OK",
                },
                {"seq": 7, "type": "verify", "ok": True},
            ]
        )
        report = shadow_replay(_run(), events, [_repair_call()], scenario=SCENARIO)

        self.assertTrue(report.ok, report.mismatches)
        self.assertTrue(report.gate.receipt_issued)
        self.assertTrue(report.provenance.uses_fallback)
        self.assertTrue(report.provenance.preserves_chain_identity)
        self.assertEqual(report.provenance.chain_backend, "primary")
        self.assertEqual(report.provenance.responder_backend, "secondary")

    def test_missing_early_nudges_are_reconstructed(self) -> None:
        run = _run(
            status="running",
            phase="leased",
            end_reason=None,
            diagnosis=None,
            diagnosis_correct=None,
            diagnosis_attempts=0,
            diagnosis_first_attempt_correct=None,
            approval_granted=0,
            repair=None,
            verify_ok=None,
            fault_present=None,
        )
        messages = [
            {
                "seq": 1,
                "role": "user",
                "content": "You have not submitted a diagnosis. Call submit_diagnosis with one exact root cause label.",
            },
            {
                "seq": 2,
                "role": "user",
                "content": "Stop investigating now. Call submit_diagnosis with your best exact root_cause label.",
            },
        ]
        report = shadow_replay(run, [_approval(seq=5)], messages=messages, scenario=SCENARIO)

        self.assertEqual(report.gate.nudge_count, 2)
        self.assertEqual(
            [event.payload.get("reconstructed") for event in report.gate_events if event.type.value == "nudge"],
            [True, True],
        )

    def test_later_denial_clears_receipt(self) -> None:
        run = _run(
            status="running",
            phase="leased",
            end_reason=None,
            diagnosis="wrong_label",
            diagnosis_correct=0,
            diagnosis_attempts=2,
            diagnosis_first_attempt_correct=0,
            approval_granted=0,
            repair=None,
            verify_ok=None,
            fault_present=None,
        )
        events = [
            _approval(seq=1),
            {
                "seq": 2,
                "type": "tool",
                "name": "restart_member",
                "status": "failed",
                "mutating": True,
                "args": {},
                "result": "ERROR: boom",
            },
            _approval(
                seq=3,
                granted=False,
                diagnosis="wrong_label",
                diagnosis_correct=False,
                diagnosis_attempts=2,
                diagnosis_first_attempt_correct=False,
            ),
        ]
        report = shadow_replay(run, events, scenario=SCENARIO)

        self.assertIs(report.gate.phase, GatePhase.ESCALATED)
        self.assertFalse(report.gate.receipt_issued)
        self.assertFalse(report.gate.diagnosis_correct)
        self.assertIsNone(report.gate.repair_name)
        self.assertIsNone(report.gate.repair_status)
        self.assertEqual(report.gate.diagnosis_attempts, 2)
        self.assertEqual(report.gate.end_reason, "diagnosis_attempts_exhausted")

    def test_prompt_mode_decline_withholds_receipt(self) -> None:
        run = _run(
            status="running",
            phase="leased",
            end_reason=None,
            diagnosis="etcd_config_error",
            diagnosis_correct=1,
            diagnosis_attempts=1,
            diagnosis_first_attempt_correct=1,
            approval_granted=0,
            repair=None,
            verify_ok=None,
            fault_present=None,
            reset_ok=None,
            baseline_ok=None,
            inject_ok=None,
        )
        events = [
            _approval(
                granted=False,
                diagnosis_correct=True,
                diagnosis_attempts=1,
                mode="prompt",
            )
        ]
        report = shadow_replay(run, events, scenario=SCENARIO)

        self.assertIs(report.gate.phase, GatePhase.AWAIT_DIAGNOSIS)
        self.assertFalse(report.gate.receipt_issued)
        self.assertTrue(report.gate.diagnosis_correct)
        self.assertTrue(report.ok, report.mismatches)

    def test_legacy_tool_statuses_map_to_typed_outcomes(self) -> None:
        cases = (
            ("finished", "OK", None, "finished", ToolResultCode.SUCCESS.value),
            ("failed", "ERROR: boom", None, "failed", "failed"),
            ("finished", "timeout waiting for pod", None, "failed", "timeout"),
            ("denied", "BLOCKED: approval not granted", None, "denied", ToolResultCode.DENIED.value),
            ("unknown", "", None, "failed", ToolResultCode.UNKNOWN_EFFECT.value),
        )
        for status, result, error, expected_status, expected_code in cases:
            with self.subTest(status=status, result=result):
                facts = classify_repair_result(status, result, error)
                report = shadow_replay(
                    _run(status="running", phase="leased", end_reason=None),
                    [],
                    [_repair_call(status=status, result_sent=result, result_raw=result, error=error)],
                    scenario=SCENARIO,
                )
                state = report.tools["call-1"]
                self.assertEqual(facts.status, expected_code)
                self.assertEqual(state.persisted_status, expected_status)
                self.assertEqual(state.result_code, expected_code)

    def test_operator_abort_maps_to_run_abort(self) -> None:
        run = _run(
            status="aborted",
            phase="aborted",
            end_reason="operator_abort",
            diagnosis=None,
            diagnosis_correct=None,
            diagnosis_attempts=0,
            diagnosis_first_attempt_correct=None,
            approval_granted=0,
            repair=None,
            verify_ok=None,
            reset_ok=1,
            baseline_ok=1,
            inject_ok=None,
            fault_present=None,
        )
        events = [
            {"seq": 1, "type": "reset", "ok": True},
            {"seq": 2, "type": "baseline", "ok": True},
            {"seq": 3, "type": "operator_abort", "reason": "operator_abort"},
        ]
        report = shadow_replay(run, events, scenario=SCENARIO)

        self.assertIs(report.run.phase, RunPhase.ABORTED)
        self.assertEqual(report.run.end_reason, "operator_abort")
        self.assertTrue(report.ok, report.mismatches)

    def test_scalar_defaults_without_trace_evidence_are_ignored(self) -> None:
        run = _run(
            status="aborted",
            phase="aborted",
            end_reason="abandoned",
            diagnosis=None,
            diagnosis_correct=0,
            diagnosis_attempts=None,
            diagnosis_first_attempt_correct=None,
            approval_granted=0,
            repair=None,
            verify_ok=0,
            reset_ok=None,
            baseline_ok=None,
            inject_ok=None,
            fault_present=None,
        )
        events = [{"seq": 1, "type": "operator_abort", "reason": "abandoned"}]
        report = shadow_replay(run, events, scenario=SCENARIO)

        self.assertIs(report.run.phase, RunPhase.ABORTED)
        self.assertTrue(report.ok, report.mismatches)

    def test_legacy_end_reason_semantics_are_reported(self) -> None:
        events = _setup_events()
        events.extend(
            [
                _approval(),
                {
                    "seq": 6,
                    "type": "tool",
                    "name": "restart_member",
                    "status": "finished",
                    "mutating": True,
                    "args": {},
                    "result": "OK",
                },
                {"seq": 7, "type": "verify", "ok": True},
            ]
        )
        report = shadow_replay(_run(end_reason="budget_tokens"), events, [_repair_call()], scenario=SCENARIO)

        self.assertEqual(report.run.end_reason, "verify_ok")
        self.assertFalse(report.ok)
        self.assertIn(
            ("run", "end_reason", "verify_ok", "budget_tokens"),
            [(item.machine, item.field, item.expected, item.actual) for item in report.mismatches],
        )


class GeneratedDiagramTests(unittest.TestCase):
    def test_generated_reference_matches_tables(self) -> None:
        document = build_document()
        path = ROOT / "docs" / "STATE_MACHINE.md"
        self.assertEqual(path.read_text(encoding="utf-8"), document)
        for marker in ("## Gate", "## Run", "## Tool", "do not edit by hand"):
            self.assertIn(marker, document)
        for state in ("await_diagnosis", "agent_running", "unknown_effect", "resume_supersede"):
            self.assertIn(state, document)


if __name__ == "__main__":
    unittest.main()
