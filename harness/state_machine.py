from __future__ import annotations

from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Generic, TypeVar

S = TypeVar("S", bound=Hashable)
I = TypeVar("I", bound=Hashable)
E = TypeVar("E", bound=Hashable)
C = TypeVar("C", bound=Hashable)


class MachineKind(StrEnum):
    GATE = "gate"
    RUN = "run"
    TOOL = "tool"


class EffectKind(StrEnum):
    APPEND_EVENT = "append_event"
    SET_PROJECTION = "set_projection"
    SET_END_REASON = "set_end_reason"
    ISSUE_RECEIPT = "issue_receipt"
    RECORD_ATTEMPT = "record_attempt"
    RECORD_VIOLATION = "record_violation"
    EMIT_NUDGE = "emit_nudge"
    ACQUIRE_LEASE = "acquire_lease"
    RENEW_LEASE = "renew_lease"
    RELEASE_LEASE = "release_lease"
    SUPERSEDE_TOOL_CALLS = "supersede_tool_calls"
    ABORT_AND_RETRY = "abort_and_retry"
    SCORE_PATH = "score_path"


@dataclass(frozen=True)
class Effect:
    kind: EffectKind
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Transition(Generic[S]):
    state: S
    effects: tuple[Effect, ...] = ()
    reason: str | None = None
    update: Callable[[Any, MachineEvent[Any]], Any] | None = None


@dataclass(frozen=True)
class Rejection:
    reason: str
    code: str = "illegal_transition"


@dataclass(frozen=True)
class Branch(Generic[S]):
    guard: Guard
    result: Transition[S] | Rejection
    label: str = ""


@dataclass(frozen=True)
class Guards(Generic[S]):
    branches: tuple[Branch[S], ...]
    default: Transition[S] | Rejection | None = None


@dataclass(frozen=True)
class IllegalTransition(ValueError):
    machine: MachineKind
    state: Hashable
    event: Hashable
    reason: str = "no transition cell"

    def __str__(self) -> str:
        return f"{self.machine.value}: {_wire(self.state)} + {_wire(self.event)} rejected: {self.reason}"


@dataclass(frozen=True)
class StateSpec(Generic[C]):
    value: str
    rank: int
    terminal: bool
    resumable: bool
    run_terminal: bool
    end_reason_required: bool
    crash_action: C | None
    renew_lease: bool = False


@dataclass(frozen=True)
class MachineEvent(Generic[E]):
    type: E
    payload: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None
    seq: int | None = None


Guard = Callable[[Any, MachineEvent[Any]], bool]


@dataclass(frozen=True)
class Machine(Generic[S, I, E, C]):
    kind: MachineKind
    states: Mapping[I, StateSpec[C]]
    events: tuple[E, ...]
    initial: S
    rules: Mapping[tuple[I, E], Transition[S] | Rejection | Guards[S]]
    state_id: Callable[[S], I] = field(default=lambda state: state)

    def transition(self, state: S, event: E | MachineEvent[E]) -> Transition[S]:
        return transition(self, state, event)


def _wire(value: object) -> str:
    return str(getattr(value, "value", value))


def _as_event(event: E | MachineEvent[E]) -> MachineEvent[E]:
    if isinstance(event, MachineEvent):
        return event
    return MachineEvent(event)


def _resolve(
    machine: Machine[S, I, E, C],
    state: S,
    event: MachineEvent[E],
    rule: Transition[S] | Rejection | Guards[S],
) -> Transition[S] | Rejection:
    if not isinstance(rule, Guards):
        return rule
    for branch in rule.branches:
        if branch.guard(state, event):
            return branch.result
    if rule.default is not None:
        return rule.default
    raise IllegalTransition(machine.kind, state, event.type, "no guard matched")


def transition(
    machine: Machine[S, I, E, C],
    state: S,
    event: E | MachineEvent[E],
) -> Transition[S]:
    machine_event = _as_event(event)
    state_key = machine.state_id(state)
    rule = machine.rules.get((state_key, machine_event.type))
    if rule is None:
        raise IllegalTransition(machine.kind, state_key, machine_event.type)
    resolved = _resolve(machine, state, machine_event, rule)
    if isinstance(resolved, Rejection):
        raise IllegalTransition(machine.kind, state_key, machine_event.type, resolved.reason)
    if resolved.update is not None:
        return Transition(resolved.update(state, machine_event), resolved.effects, resolved.reason)
    return resolved


def rejection_event(
    machine: Machine[S, I, E, C],
    state: S,
    event: E | MachineEvent[E],
    error: str,
) -> dict[str, Any]:
    machine_event = _as_event(event)
    return {
        "type": "state_transition_rejected",
        "payload": {
            "machine": machine.kind.value,
            "event": machine_event.type.value,
            "state_before": machine.state_id(state),
            "error": error,
        },
    }


def fold(
    machine: Machine[S, I, E, C],
    events: Iterable[E | MachineEvent[E]],
    initial: S | None = None,
) -> S:
    state = machine.initial if initial is None else initial
    seen: set[str] = set()
    last_seq: int | None = None

    for raw_event in events:
        event = _as_event(raw_event)
        dedupe_key: str | None = None
        if event.seq is not None:
            if last_seq is not None and event.seq <= last_seq:
                raise ValueError(f"events out of order: {last_seq} then {event.seq}")
            last_seq = event.seq
            dedupe_key = f"seq:{event.seq}"
        elif event.idempotency_key is not None:
            dedupe_key = f"key:{event.idempotency_key}"
        if dedupe_key is not None:
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
        state = transition(machine, state, event).state

    return state


def validate_machine(machine: Machine[S, I, E, C]) -> None:
    if machine.state_id(machine.initial) not in machine.states:
        raise ValueError(f"initial state missing from states: {_wire(machine.initial)}")

    for state_key, spec in machine.states.items():
        if spec.rank < 0:
            raise ValueError(f"negative rank for {_wire(state_key)}")
        if spec.terminal and spec.resumable:
            raise ValueError(f"terminal state is resumable: {_wire(state_key)}")
        if spec.run_terminal and not spec.end_reason_required:
            raise ValueError(f"run-terminal state lacks end reason: {_wire(state_key)}")
        if spec.resumable and spec.crash_action is None:
            raise ValueError(f"resumable state lacks crash action: {_wire(state_key)}")

    for state_key in machine.states:
        for event in machine.events:
            if (state_key, event) not in machine.rules:
                raise ValueError(f"missing transition cell: {_wire(state_key)} + {_wire(event)}")

    for (state_key, event), rule in machine.rules.items():
        if state_key not in machine.states:
            raise ValueError(f"rule state missing from states: {_wire(state_key)}")
        if event not in machine.events:
            raise ValueError(f"rule event missing from events: {_wire(event)}")
        _validate_rule(machine, rule)


def _validate_rule(
    machine: Machine[S, I, E, C],
    rule: Transition[S] | Rejection | Guards[S],
) -> None:
    if isinstance(rule, Rejection):
        return
    if isinstance(rule, Guards):
        if not rule.branches and rule.default is None:
            raise ValueError("guard rule has no branches")
        for branch in rule.branches:
            _validate_rule(machine, branch.result)
        if rule.default is not None:
            _validate_rule(machine, rule.default)
        return
    target_key = machine.state_id(rule.state)
    if target_key not in machine.states:
        raise ValueError(f"rule target missing from states: {_wire(target_key)}")
