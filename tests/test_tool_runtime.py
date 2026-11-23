from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.machines.tools import ToolCrashAction, ToolPhase
from harness.tool_runtime import ToolLedger, ToolRuntime, canonical_tool_event


def drive(runtime: ToolRuntime) -> None:
    runtime.prepare("restart_member", mutating=True)
    runtime.start()
    runtime.success()


class ToolRuntimeTests(unittest.TestCase):
    def test_happy_path_owns_status_and_result_code(self) -> None:
        logged: list[dict] = []
        runtime = ToolRuntime("call-1", log=logged.append)
        drive(runtime)
        self.assertIs(runtime.phase, ToolPhase.SUCCEEDED)
        self.assertEqual(runtime.status, "finished")
        self.assertEqual(runtime.result_code, "success")
        self.assertTrue(runtime.terminal)
        self.assertEqual(logged[-1], canonical_tool_event(runtime.events[-1], "call-1"))

    def test_snapshot_and_event_replay_agree(self) -> None:
        logged: list[dict] = []
        runtime = ToolRuntime("call-1", log=logged.append)
        drive(runtime)
        restored = ToolRuntime.from_snapshot("call-1", runtime.to_snapshot())
        replayed = ToolLedger.from_events("run-1", logged).get("call-1")
        for candidate in (restored, replayed):
            self.assertIsNotNone(candidate)
            assert candidate is not None
            self.assertIs(candidate.phase, ToolPhase.SUCCEEDED)
            self.assertEqual(candidate.status, "finished")

    def test_denial_is_typed_and_terminal(self) -> None:
        runtime = ToolRuntime("call-1")
        runtime.prepare("delete_pod", mutating=True)
        runtime.deny("forbidden", "forbidden")
        self.assertIs(runtime.phase, ToolPhase.DENIED)
        self.assertEqual(runtime.status, "denied")
        self.assertEqual(runtime.result_code, "denied")
        self.assertEqual(runtime.state.violation_kind, "forbidden")

    def test_mutating_cancel_produces_unknown_effect(self) -> None:
        runtime = ToolRuntime("call-1")
        runtime.prepare("restart_member", mutating=True)
        runtime.start()
        runtime.cancel()
        self.assertIs(runtime.phase, ToolPhase.UNKNOWN_EFFECT)
        self.assertEqual(runtime.result_code, "unknown_effect")
        self.assertIs(runtime.facts.crash_action, ToolCrashAction.NONE)

    def test_read_only_resume_supersede_is_terminal(self) -> None:
        runtime = ToolRuntime("call-1")
        runtime.prepare("member_list", mutating=False)
        runtime.resume_supersede()
        self.assertIs(runtime.phase, ToolPhase.SUPERSEDED)
        self.assertEqual(runtime.result_code, "superseded_after_resume")

    def test_ledger_infers_unfinished_mutating_rows(self) -> None:
        ledger = ToolLedger("run-1")
        ledger.merge_rows([{
            "call_id": "repair-1",
            "name": "restart_member",
            "mutating": 1,
            "destructive": 0,
            "status": "started",
            "phase": "started",
            "result_code": None,
        }])
        self.assertTrue(ledger.has_unknown_mutating())
        self.assertIn("repair-1", ledger.unfinished())

    def test_illegal_transition_is_rejected_without_corrupt_state(self) -> None:
        runtime = ToolRuntime("call-1")
        runtime.prepare("member_list", mutating=False)
        runtime.success()
        self.assertIs(runtime.phase, ToolPhase.INTENDED)
        self.assertTrue(runtime.rejections)


if __name__ == "__main__":
    unittest.main()
