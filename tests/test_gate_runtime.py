from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.vocab import RESTART_MEMBER
from harness.gate_runtime import GateRuntime, canonical_gate_event
from harness.machines.gate import GateEndReason, GateEvent, GatePhase, GateViolation
from harness.tooling import AgentState, Diagnosis, Repair
from tools.tool import Tool


def diagnosis_tool() -> Tool:
    return Tool("submit_diagnosis", "", kind="diagnosis")


def repair_tool() -> Tool:
    return Tool(RESTART_MEMBER, "", kind="repair", mutating=True)


def grant(state: AgentState) -> None:
    Diagnosis(state).execute(diagnosis_tool(), {"root_cause": state.scenario.root_cause}, "call-1")


class GateRuntimeTests(unittest.TestCase):
    def test_correct_approved_diagnosis_derives_receipt(self) -> None:
        runtime = GateRuntime("run-1")
        runtime.submit_diagnosis(
            label="etcd_member_unreachable",
            correct=True,
            approved=True,
            attempts=1,
            first_attempt_correct=True,
            is_no_fault=False,
            source="policy",
        )
        self.assertEqual(runtime.state.phase, GatePhase.APPROVED)
        self.assertEqual(runtime.receipt, "approved:run-1")
        self.assertEqual(runtime.emission()[0].type, GateEvent.DIAGNOSIS_SUBMITTED)

    def test_prompt_decline_keeps_correctness_but_withholds_receipt(self) -> None:
        runtime = GateRuntime("run-1")
        runtime.submit_diagnosis(
            label="etcd_member_unreachable",
            correct=True,
            approved=False,
            attempts=1,
            first_attempt_correct=True,
            is_no_fault=False,
            source="operator",
        )
        self.assertEqual(runtime.state.phase, GatePhase.AWAIT_DIAGNOSIS)
        self.assertTrue(runtime.state.diagnosis_correct)
        self.assertIsNone(runtime.receipt)

    def test_nudge_cap_converts_to_budget_exhausted(self) -> None:
        runtime = GateRuntime("run-1")
        runtime.nudge(step=1, kind="no_tool")
        runtime.nudge(step=2, kind="budget")
        runtime.nudge(step=3, kind="budget")
        self.assertEqual(runtime.state.phase, GatePhase.ESCALATED)
        self.assertEqual(runtime.state.end_reason, GateEndReason.BUDGET_TOKENS.value)
        self.assertEqual(
            [event.type for event in runtime.emission()],
            [GateEvent.NUDGE, GateEvent.NUDGE, GateEvent.BUDGET_EXHAUSTED],
        )

    def test_repair_proposal_and_result_reach_repaired(self) -> None:
        scenario = {"acceptable_repairs": [RESTART_MEMBER], "forbidden_repairs": []}
        runtime = GateRuntime("run-1")
        runtime.submit_diagnosis(
            label="etcd_member_unreachable",
            correct=True,
            approved=True,
            attempts=1,
            first_attempt_correct=True,
            is_no_fault=False,
            source="policy",
        )
        runtime.propose_repair(RESTART_MEMBER, {"node": "vm1"}, scenario)
        runtime.finish_repair(status="success", result="restart ok", idempotency_key="repair-result:vm1")
        self.assertEqual(runtime.state.phase, GatePhase.REPAIRED)
        self.assertEqual(runtime.state.repair_name, RESTART_MEMBER)

    def test_illegal_transition_is_rejected_without_corrupt_state(self) -> None:
        runtime = GateRuntime("run-1")
        runtime.finish_repair(status="success", result="too early", idempotency_key="repair-result:early")
        self.assertEqual(runtime.state.phase, GatePhase.AWAIT_DIAGNOSIS)
        self.assertTrue(runtime.rejections)
        self.assertFalse(runtime.events)

    def test_snapshot_replays_canonical_gate_events(self) -> None:
        logged: list[dict] = []
        runtime = GateRuntime("run-1", log=logged.append)
        runtime.submit_diagnosis(
            label="etcd_member_unreachable",
            correct=True,
            approved=True,
            attempts=1,
            first_attempt_correct=True,
            is_no_fault=False,
            source="policy",
        )
        self.assertEqual(logged[0], canonical_gate_event(runtime.events[0]))
        restored = GateRuntime.from_snapshot("run-1", runtime.to_snapshot())
        self.assertEqual(restored.state.phase, GatePhase.APPROVED)
        self.assertEqual(restored.receipt, "approved:run-1")
        replayed = GateRuntime.from_events("run-1", logged)
        self.assertEqual(replayed.state.phase, GatePhase.APPROVED)


class AgentStateGateRuntimeTests(unittest.TestCase):
    def test_gate_fields_survive_snapshot(self) -> None:
        scenario = {"root_cause": "etcd_member_unreachable"}
        state = AgentState(scenario, "run-1", "policy")
        grant(state)
        snapshot = state.snapshot("trace", 1, 1, 10, False)
        restored = AgentState.from_snapshot(snapshot, scenario, "run-1", "policy")
        self.assertEqual(restored.gate.state.phase, GatePhase.APPROVED)
        self.assertEqual(restored.receipt, "approved:run-1")
        self.assertTrue(restored.diagnosis_correct)

    def test_denied_repair_records_gate_violation(self) -> None:
        scenario = {
            "root_cause": "etcd_member_unreachable",
            "acceptable_repairs": [],
            "forbidden_repairs": [RESTART_MEMBER],
        }
        state = AgentState(scenario, "run-2", "policy")
        grant(state)
        decision = Repair(state, None).authorize(repair_tool(), {"node": "vm1"})
        self.assertFalse(decision.ok)
        self.assertIn(GateViolation.FORBIDDEN_REPAIR, state.gate.state.violations)
        self.assertIn("forbidden", decision.unwrap().reason)

    def test_prompt_decision_updates_gate(self) -> None:
        state = AgentState({"root_cause": "etcd_member_unreachable"}, "run-3", "prompt")
        with patch("builtins.input", return_value="n"):
            outcome = Diagnosis(state).execute(diagnosis_tool(), {"root_cause": "etcd_member_unreachable"}, "call-1")
        self.assertFalse(hasattr(outcome, "approval_token"))
        self.assertTrue(state.gate.state.diagnosis_correct)
        self.assertIsNone(state.receipt)

    def test_repair_authorizes_from_gate_state(self) -> None:
        scenario = {
            "root_cause": "etcd_member_unreachable",
            "acceptable_repairs": [RESTART_MEMBER],
            "forbidden_repairs": [],
        }
        state = AgentState(scenario, "run-4", "policy")
        grant(state)
        decision = Repair(state, None).authorize(repair_tool(), {"node": "vm1"})
        self.assertTrue(decision.ok)

    def test_repair_blocks_without_gate_receipt(self) -> None:
        state = AgentState(
            {
                "root_cause": "etcd_member_unreachable",
                "acceptable_repairs": [RESTART_MEMBER],
                "forbidden_repairs": [],
            },
            "run-5",
            "policy",
        )
        state.approval_token = "approved:run-5"
        decision = Repair(state, None).authorize(repair_tool(), {"node": "vm1", "approval_token": "approved:run-5"})
        self.assertFalse(decision.ok)
        self.assertIsNone(state.receipt)
        self.assertIn(GateViolation.PREMATURE_REPAIR, state.gate.state.violations)


if __name__ == "__main__":
    unittest.main()
