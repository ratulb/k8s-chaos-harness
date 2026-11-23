from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.machines.run import RunCrashAction, RunEndReason, RunPhase
from harness.run_runtime import RunRuntime, canonical_run_event


def drive(runtime: RunRuntime) -> None:
    runtime.create()
    runtime.lease()
    runtime.begin_reset()
    runtime.reset_ok()
    runtime.baseline_ok()
    runtime.inject_ok()
    runtime.fault_observed()
    runtime.agent_done(gate_terminal="repaired")


class RunRuntimeTests(unittest.TestCase):
    def test_happy_path_owns_lifecycle_and_terminal_outcome(self) -> None:
        runtime = RunRuntime("run-1")
        drive(runtime)
        self.assertIs(runtime.state.phase, RunPhase.VERIFYING)
        runtime.verified()
        self.assertIs(runtime.state.phase, RunPhase.FINISHED)
        self.assertEqual(runtime.end_reason, RunEndReason.VERIFY_OK.value)
        self.assertTrue(runtime.verify_ok)
        self.assertTrue(runtime.terminal)
        self.assertFalse(runtime.renew_lease)

    def test_snapshot_and_event_log_replay_the_same_state(self) -> None:
        logged: list[dict] = []
        runtime = RunRuntime("run-1", log=logged.append)
        drive(runtime)
        runtime.verification_failed()
        self.assertEqual(logged[-1], canonical_run_event(runtime.events[-1]))
        restored = RunRuntime.from_snapshot("run-1", runtime.to_snapshot())
        replayed = RunRuntime.from_events("run-1", logged)
        for candidate in (restored, replayed):
            self.assertIs(candidate.state.phase, RunPhase.FINISHED)
            self.assertEqual(candidate.end_reason, RunEndReason.VERIFY_FAILED.value)
            self.assertFalse(candidate.verify_ok)

    def test_resume_action_uses_parked_previous_state(self) -> None:
        runtime = RunRuntime("run-1")
        drive(runtime)
        runtime.lease_expired(RunPhase.AGENT_RUNNING, 123.0)
        self.assertIs(runtime.state.phase, RunPhase.INTERRUPTED)
        self.assertIs(runtime.resume_action(), RunCrashAction.RESUME)
        self.assertIs(runtime.resume_action(unknown_effect=True), RunCrashAction.ABORT_AND_RETRY)

    def test_from_run_reconstructs_agent_running(self) -> None:
        runtime = RunRuntime.from_run("run-1", {"status": "running", "phase": "agent_running"})
        self.assertIs(runtime.state.phase, RunPhase.AGENT_RUNNING)
        self.assertFalse(runtime.rejections)

    def test_illegal_transition_is_rejected_without_corrupt_state(self) -> None:
        runtime = RunRuntime("run-1")
        runtime.create()
        runtime.lease()
        runtime.verified()
        self.assertIs(runtime.state.phase, RunPhase.LEASED)
        self.assertTrue(runtime.rejections)

    def test_abort_and_lease_gating_follow_state(self) -> None:
        runtime = RunRuntime("run-1")
        runtime.create()
        runtime.lease()
        self.assertTrue(runtime.renew_lease)
        runtime.abort("operator_abort")
        self.assertIs(runtime.state.phase, RunPhase.ABORTED)
        self.assertEqual(runtime.end_reason, "operator_abort")
        self.assertFalse(runtime.renew_lease)
        self.assertTrue(runtime.terminal)


if __name__ == "__main__":
    unittest.main()
