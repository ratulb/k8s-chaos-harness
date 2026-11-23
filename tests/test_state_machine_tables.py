from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.machines import (
    GATE_MACHINE,
    RUN_MACHINE,
    TOOL_MACHINE,
    GateEvent,
    GatePhase,
    GateViolation,
    RunEvent,
    RunPhase,
    ToolEvent,
    ToolPhase,
)
from harness.machines.run import RunCrashAction, resume_triage
from harness.machines.tools import ToolResultCode
from harness.state_machine import (
    EffectKind,
    IllegalTransition,
    Machine,
    MachineEvent,
    transition,
    validate_machine,
)


def _apply(machine: Machine, state, events: list[MachineEvent]):
    result = None
    for event in events:
        result = transition(machine, state, event)
        state = result.state
    return state, result


class MachineTableTests(unittest.TestCase):
    def test_all_machines_have_explicit_cell_coverage(self) -> None:
        for machine in (GATE_MACHINE, RUN_MACHINE, TOOL_MACHINE):
            with self.subTest(machine=machine.kind):
                validate_machine(machine)

    def test_gate_correct_diagnosis_and_no_fault_paths(self) -> None:
        event = MachineEvent(
            GateEvent.DIAGNOSIS_SUBMITTED,
            {
                "label": "etcd_member_unreachable",
                "correct": True,
                "is_no_fault": False,
                "attempts": 1,
                "first_attempt_correct": True,
                "source": "model",
            },
        )
        result = transition(GATE_MACHINE, GATE_MACHINE.initial, event)
        self.assertIs(result.state.phase, GatePhase.APPROVED)
        self.assertTrue(result.state.receipt_issued)
        self.assertIn(EffectKind.ISSUE_RECEIPT, [effect.kind for effect in result.effects])

        control = MachineEvent(
            GateEvent.DIAGNOSIS_SUBMITTED,
            {"label": "no_fault", "correct": True, "is_no_fault": True, "attempts": 1},
        )
        state, _result = _apply(GATE_MACHINE, GATE_MACHINE.initial, [control])
        self.assertIs(state.phase, GatePhase.CLEARED)
        self.assertTrue(state.is_no_fault)

    def test_gate_wrong_twice_escalates_and_nudge_cap_rejects(self) -> None:
        wrong_one = MachineEvent(GateEvent.DIAGNOSIS_SUBMITTED, {"correct": False, "attempts": 1})
        wrong_two = MachineEvent(GateEvent.DIAGNOSIS_SUBMITTED, {"correct": False, "attempts": 2})
        state, result = _apply(GATE_MACHINE, GATE_MACHINE.initial, [wrong_one, wrong_two])
        self.assertIs(state.phase, GatePhase.ESCALATED)
        self.assertEqual(state.end_reason, "diagnosis_attempts_exhausted")
        self.assertIn(EffectKind.SET_END_REASON, [effect.kind for effect in result.effects])

        nudges = [MachineEvent(GateEvent.NUDGE, {"step": index}) for index in (1, 2, 3)]
        state = GATE_MACHINE.initial
        for nudge in nudges[:2]:
            state = transition(GATE_MACHINE, state, nudge).state
        self.assertEqual(state.nudge_count, 2)
        with self.assertRaises(IllegalTransition):
            transition(GATE_MACHINE, state, nudges[2])

    def test_gate_violations_and_unknown_effect(self) -> None:
        diagnosis = MachineEvent(GateEvent.DIAGNOSIS_SUBMITTED, {"correct": True, "is_no_fault": True, "attempts": 1})
        repair = MachineEvent(GateEvent.REPAIR_PROPOSED, {"canonical_name": "restart_etcd", "acceptable": True})
        state, _result = _apply(GATE_MACHINE, GATE_MACHINE.initial, [diagnosis, repair])
        self.assertIs(state.phase, GatePhase.CLEARED)
        self.assertIn(GateViolation.REPAIR_AFTER_NO_FAULT, state.violations)

        fault = MachineEvent(GateEvent.DIAGNOSIS_SUBMITTED, {"correct": True, "is_no_fault": False, "attempts": 1})
        approved = transition(GATE_MACHINE, GATE_MACHINE.initial, fault).state
        unknown = transition(GATE_MACHINE, approved, MachineEvent(GateEvent.UNKNOWN_EFFECT, {"tool_call_id": "call-1"}))
        self.assertIs(unknown.state.phase, GatePhase.ESCALATED)
        self.assertEqual(unknown.state.end_reason, "unknown_effect")
        self.assertIn(EffectKind.ABORT_AND_RETRY, [effect.kind for effect in unknown.effects])

    def test_run_success_path_projects_finished(self) -> None:
        events = [
            MachineEvent(RunEvent.CREATE),
            MachineEvent(RunEvent.LEASE, {"lease_owner": "runner", "lease_expires_at": 100}),
            MachineEvent(RunEvent.BEGIN_RESET),
            MachineEvent(RunEvent.RESET_OK),
            MachineEvent(RunEvent.BASELINE_OK),
            MachineEvent(RunEvent.INJECT_OK),
            MachineEvent(RunEvent.FAULT_OBSERVED),
            MachineEvent(RunEvent.AGENT_DONE, {"gate_terminal": "repaired", "gate_reason": None}),
            MachineEvent(RunEvent.VERIFY_OK),
        ]
        state, result = _apply(RUN_MACHINE, RUN_MACHINE.initial, events)
        self.assertIs(state.phase, RunPhase.FINISHED)
        self.assertEqual(state.status, "finished")
        self.assertEqual(state.wire_phase, "finished")
        self.assertEqual(state.end_reason, "verify_ok")
        self.assertTrue(state.verify_ok)
        self.assertTrue(state.fault_present)
        self.assertIn(EffectKind.RELEASE_LEASE, [effect.kind for effect in result.effects])

    def test_run_lease_expiry_is_idempotent_by_key(self) -> None:
        leased = transition(
            RUN_MACHINE,
            RUN_MACHINE.initial,
            MachineEvent(RunEvent.LEASE, {"lease_owner": "runner", "lease_expires_at": 100}),
        ).state
        expired = MachineEvent(
            RunEvent.LEASE_EXPIRED,
            {
                "reason": "lease_expired",
                "previous_state": "leased",
                "lease_expires_at": 100,
                "idempotency_key": "reap:1",
            },
        )
        interrupted = transition(RUN_MACHINE, leased, expired).state
        self.assertIs(interrupted.phase, RunPhase.INTERRUPTED)
        self.assertIs(interrupted.previous_state, RunPhase.LEASED)
        self.assertEqual(interrupted.idempotency_key, "reap:1")
        self.assertIs(transition(RUN_MACHINE, interrupted, expired).state, interrupted)

        different = MachineEvent(
            RunEvent.LEASE_EXPIRED,
            {
                "reason": "lease_expired",
                "previous_state": "leased",
                "lease_expires_at": 100,
                "idempotency_key": "reap:2",
            },
        )
        with self.assertRaises(IllegalTransition):
            transition(RUN_MACHINE, interrupted, different)

    def test_run_resume_triage_table(self) -> None:
        self.assertEqual(resume_triage(RunPhase.RESETTING), RunCrashAction.RESUME_RELEASE)
        self.assertEqual(resume_triage(RunPhase.INJECTING), RunCrashAction.NEW_RUN_AFTER_RESET)
        self.assertEqual(resume_triage(RunPhase.AGENT_RUNNING), RunCrashAction.RESUME)
        self.assertEqual(resume_triage(RunPhase.AGENT_RUNNING, True), RunCrashAction.ABORT_AND_RETRY)
        self.assertEqual(resume_triage(RunPhase.VERIFYING), RunCrashAction.RERUN_VERIFY)

    def test_tool_mutating_cancel_and_read_only_supersede(self) -> None:
        prepare_mutating = MachineEvent(ToolEvent.PREPARE, {"name": "repair", "mutating": True, "destructive": False})
        intended = transition(TOOL_MACHINE, TOOL_MACHINE.initial, prepare_mutating).state
        started = transition(TOOL_MACHINE, intended, MachineEvent(ToolEvent.START)).state
        cancelled = transition(TOOL_MACHINE, started, MachineEvent(ToolEvent.CANCEL, {"reason": "crash"}))
        self.assertIs(cancelled.state.phase, ToolPhase.UNKNOWN_EFFECT)
        self.assertEqual(cancelled.state.persisted_status, "failed")
        self.assertEqual(cancelled.state.result_code, ToolResultCode.UNKNOWN_EFFECT.value)
        self.assertIn(EffectKind.ABORT_AND_RETRY, [effect.kind for effect in cancelled.effects])

        prepare_read_only = MachineEvent(ToolEvent.PREPARE, {"name": "diagnose", "mutating": False})
        read_only = transition(TOOL_MACHINE, TOOL_MACHINE.initial, prepare_read_only).state
        read_only_started = transition(TOOL_MACHINE, read_only, MachineEvent(ToolEvent.START)).state
        with self.assertRaises(IllegalTransition):
            transition(TOOL_MACHINE, read_only_started, MachineEvent(ToolEvent.DENY, {"reason": "late"}))
        superseded = transition(
            TOOL_MACHINE,
            read_only_started,
            MachineEvent(ToolEvent.RESUME_SUPERSEDE, {"reason": "resume"}),
        ).state
        self.assertIs(superseded.phase, ToolPhase.SUPERSEDED)
        self.assertEqual(superseded.result_code, ToolResultCode.SUPERSEDED.value)
        self.assertEqual(superseded.persisted_status, "finished")

    def test_tool_mutating_supersede_is_rejected(self) -> None:
        prepare = MachineEvent(ToolEvent.PREPARE, {"name": "repair", "mutating": True})
        intended = transition(TOOL_MACHINE, TOOL_MACHINE.initial, prepare).state
        with self.assertRaises(IllegalTransition):
            transition(TOOL_MACHINE, intended, MachineEvent(ToolEvent.RESUME_SUPERSEDE, {"reason": "resume"}))


if __name__ == "__main__":
    unittest.main()
