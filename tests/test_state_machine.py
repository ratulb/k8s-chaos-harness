from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from enum import StrEnum
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.state_machine import (
    Effect,
    EffectKind,
    IllegalTransition,
    Machine,
    MachineEvent,
    MachineKind,
    Rejection,
    StateSpec,
    Transition,
    fold,
    transition,
    validate_machine,
)


class LightState(StrEnum):
    RED = "red"
    GREEN = "green"
    YELLOW = "yellow"
    DONE = "done"


class LightEvent(StrEnum):
    TICK = "tick"
    RESET = "reset"
    STOP = "stop"


class CrashAction(StrEnum):
    RESUME = "resume"


STATES = {
    LightState.RED: StateSpec("red", 0, False, True, False, False, CrashAction.RESUME),
    LightState.GREEN: StateSpec("green", 1, False, True, False, False, CrashAction.RESUME),
    LightState.YELLOW: StateSpec("yellow", 2, False, True, False, False, CrashAction.RESUME),
    LightState.DONE: StateSpec("done", 3, True, False, True, True, None),
}

EVENTS = (LightEvent.TICK, LightEvent.RESET, LightEvent.STOP)

RULES = {
    (LightState.RED, LightEvent.TICK): Transition(
        LightState.GREEN,
        (Effect(EffectKind.SET_PROJECTION, {"phase": "green"}),),
        "tick",
    ),
    (LightState.GREEN, LightEvent.TICK): Transition(LightState.YELLOW),
    (LightState.YELLOW, LightEvent.TICK): Transition(LightState.RED),
    (LightState.RED, LightEvent.RESET): Transition(LightState.RED),
    (LightState.GREEN, LightEvent.RESET): Transition(LightState.RED),
    (LightState.YELLOW, LightEvent.RESET): Transition(LightState.RED),
    (LightState.RED, LightEvent.STOP): Transition(LightState.DONE, reason="operator_stop"),
    (LightState.GREEN, LightEvent.STOP): Transition(LightState.DONE, reason="operator_stop"),
    (LightState.YELLOW, LightEvent.STOP): Transition(LightState.DONE, reason="operator_stop"),
    (LightState.DONE, LightEvent.TICK): Rejection("terminal"),
    (LightState.DONE, LightEvent.RESET): Rejection("terminal"),
    (LightState.DONE, LightEvent.STOP): Rejection("terminal"),
}


def _machine(
    rules: dict | None = None,
    states: dict | None = None,
) -> Machine:
    return Machine(
        kind=MachineKind.GATE,
        states=STATES if states is None else states,
        events=EVENTS,
        initial=LightState.RED,
        rules=RULES if rules is None else rules,
    )


class StateMachineCoreTests(unittest.TestCase):
    def test_transition_is_pure_and_returns_effects(self) -> None:
        machine = _machine()
        event = MachineEvent(LightEvent.TICK, {"source": "test"}, seq=1)
        first = transition(machine, LightState.RED, event)
        second = machine.transition(LightState.RED, LightEvent.TICK)

        self.assertEqual(first, second)
        self.assertIs(first.state, LightState.GREEN)
        self.assertEqual(first.reason, "tick")
        self.assertEqual(first.effects[0].kind, EffectKind.SET_PROJECTION)
        self.assertEqual(first.effects[0].payload, {"phase": "green"})
        self.assertEqual(event.payload, {"source": "test"})

    def test_explicit_rejection_and_missing_cell_raise_illegal_transition(self) -> None:
        machine = _machine()
        with self.assertRaises(IllegalTransition) as rejected:
            transition(machine, LightState.DONE, LightEvent.TICK)
        self.assertEqual(rejected.exception.reason, "terminal")
        self.assertIn("done + tick rejected", str(rejected.exception))

        rules = dict(RULES)
        del rules[(LightState.GREEN, LightEvent.TICK)]
        with self.assertRaises(IllegalTransition) as missing:
            transition(_machine(rules=rules), LightState.GREEN, LightEvent.TICK)
        self.assertEqual(missing.exception.reason, "no transition cell")

    def test_fold_is_deterministic_and_skips_duplicate_keys(self) -> None:
        machine = _machine()
        events = [
            MachineEvent(LightEvent.TICK, idempotency_key="tick-1"),
            MachineEvent(LightEvent.TICK, idempotency_key="tick-1"),
            MachineEvent(LightEvent.TICK, idempotency_key="tick-2"),
        ]
        self.assertEqual(fold(machine, events), LightState.YELLOW)
        self.assertEqual(fold(machine, events[:1] + events[2:]), LightState.YELLOW)

    def test_fold_rejects_out_of_order_seq(self) -> None:
        machine = _machine()
        events = [
            MachineEvent(LightEvent.TICK, seq=2),
            MachineEvent(LightEvent.TICK, seq=1),
        ]
        with self.assertRaisesRegex(ValueError, "events out of order"):
            fold(machine, events)

    def test_validate_machine_accepts_full_table(self) -> None:
        validate_machine(_machine())

    def test_validate_machine_flags_missing_cells_and_bad_specs(self) -> None:
        rules = dict(RULES)
        del rules[(LightState.GREEN, LightEvent.TICK)]
        with self.assertRaisesRegex(ValueError, "missing transition cell"):
            validate_machine(_machine(rules=rules))

        states = dict(STATES)
        states[LightState.RED] = replace(
            STATES[LightState.RED],
            terminal=True,
            resumable=True,
        )
        with self.assertRaisesRegex(ValueError, "terminal state is resumable"):
            validate_machine(_machine(states=states))


if __name__ == "__main__":
    unittest.main()
