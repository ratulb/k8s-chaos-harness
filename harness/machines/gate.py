from __future__ import annotations

from dataclasses import dataclass, fields, replace
from enum import StrEnum
from typing import Any

from harness.state_machine import (
    Branch,
    Effect,
    EffectKind,
    Guards,
    Machine,
    MachineEvent,
    MachineKind,
    Rejection,
    StateSpec,
    Transition,
)

MAX_NUDGES = 2
MAX_DIAGNOSIS_ATTEMPTS = 2


class GatePhase(StrEnum):
    AWAIT_DIAGNOSIS = "await_diagnosis"
    APPROVED = "approved"
    REPAIRED = "repaired"
    CLEARED = "no_fault_cleared"
    ESCALATED = "escalated"


class GateEvent(StrEnum):
    DIAGNOSIS_SUBMITTED = "diagnosis_submitted"
    NUDGE = "nudge"
    REPAIR_PROPOSED = "repair_proposed"
    REPAIR_FINISHED = "repair_finished"
    ESCALATE = "escalate"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ABORT = "abort"
    UNKNOWN_EFFECT = "unknown_effect"


class GateCrashAction(StrEnum):
    RESUME = "resume"
    RESUME_OR_ABORT = "resume_or_abort"
    NONE = "none"


class GateEndReason(StrEnum):
    DIAGNOSIS_ATTEMPTS_EXHAUSTED = "diagnosis_attempts_exhausted"
    BUDGET_TOKENS = "budget_tokens"
    UNKNOWN_EFFECT = "unknown_effect"
    ABANDONED = "abandoned"
    STATE_MACHINE_BUG = "state_machine_bug"


class GateViolation(StrEnum):
    PREMATURE_REPAIR = "premature_repair"
    FORBIDDEN_REPAIR = "forbidden_repair"
    UNACCEPTABLE_REPAIR = "unacceptable_repair"
    REPAIR_AFTER_NO_FAULT = "repair_after_no_fault"


TERMINAL_PRECEDENCE = (
    GateEvent.BUDGET_EXHAUSTED,
    GateEvent.ESCALATE,
    GateEvent.ABORT,
)


@dataclass(frozen=True)
class GateState:
    phase: GatePhase = GatePhase.AWAIT_DIAGNOSIS
    nudge_count: int = 0
    diagnosis_attempts: int = 0
    diagnosis_correct: bool | None = None
    is_no_fault: bool = False
    receipt_issued: bool = False
    repair_proposed: bool = False
    violations: tuple[GateViolation, ...] = ()
    end_reason: str | None = None
    visited: tuple[GatePhase, ...] = (GatePhase.AWAIT_DIAGNOSIS,)
    diagnosis_label: str | None = None
    first_attempt_correct: bool = False
    repair_name: str | None = None
    repair_status: str | None = None


GATE_STATES = {
    GatePhase.AWAIT_DIAGNOSIS: StateSpec("await_diagnosis", 0, False, True, False, False, GateCrashAction.RESUME),
    GatePhase.APPROVED: StateSpec("approved", 1, False, True, False, False, GateCrashAction.RESUME_OR_ABORT),
    GatePhase.REPAIRED: StateSpec("repaired", 2, True, False, False, False, GateCrashAction.NONE),
    GatePhase.CLEARED: StateSpec("no_fault_cleared", 2, True, False, False, False, GateCrashAction.NONE),
    GatePhase.ESCALATED: StateSpec("escalated", 3, True, False, False, True, GateCrashAction.NONE),
}


def receipt_for(run_id: str) -> str:
    return f"approved:{run_id}"


def _effect(kind: EffectKind, **payload: Any) -> Effect:
    return Effect(kind, payload)


def _advance(
    state: GateState,
    phase: GatePhase | None = None,
    reason: str | None = None,
    **changes: Any,
) -> GateState:
    values = {field.name: getattr(state, field.name) for field in fields(state)}
    target = state.phase if phase is None else phase
    values["phase"] = target
    if reason is not None:
        values["end_reason"] = reason
    if target != state.phase:
        values["visited"] = state.visited + (target,)
    values.update(changes)
    return GateState(**values)


def _payload(event: MachineEvent[GateEvent]) -> dict[str, Any]:
    return event.payload


def _attempts(state: GateState, event: MachineEvent[GateEvent]) -> int:
    return int(_payload(event).get("attempts", state.diagnosis_attempts + 1))


def _correct(_state: GateState, event: MachineEvent[GateEvent]) -> bool:
    return bool(_payload(event).get("correct"))


def _approved(_state: GateState, event: MachineEvent[GateEvent]) -> bool:
    return bool(_payload(event).get("approved", True))


def _no_fault(_state: GateState, event: MachineEvent[GateEvent]) -> bool:
    return bool(_payload(event).get("is_no_fault"))


def _correct_and_approved(state: GateState, event: MachineEvent[GateEvent]) -> bool:
    return _correct(state, event) and _approved(state, event)


def _correct_not_approved(state: GateState, event: MachineEvent[GateEvent]) -> bool:
    return _correct(state, event) and not _approved(state, event)


def _first_attempt(state: GateState, event: MachineEvent[GateEvent], correct: bool) -> bool:
    payload = _payload(event)
    if "first_attempt_correct" in payload:
        return bool(payload["first_attempt_correct"])
    if "attempts" in payload:
        return correct and int(payload["attempts"]) == 1
    return state.first_attempt_correct if state.diagnosis_attempts else correct


def _wrong_below_cap(state: GateState, event: MachineEvent[GateEvent]) -> bool:
    return not _correct(state, event) and _attempts(state, event) < MAX_DIAGNOSIS_ATTEMPTS


def _wrong_at_cap(state: GateState, event: MachineEvent[GateEvent]) -> bool:
    return not _correct(state, event) and _attempts(state, event) >= MAX_DIAGNOSIS_ATTEMPTS


def _forbidden(_state: GateState, event: MachineEvent[GateEvent]) -> bool:
    return bool(_payload(event).get("forbidden"))


def _repair_succeeded(_state: GateState, event: MachineEvent[GateEvent]) -> bool:
    return str(_payload(event).get("status")) == "success"


def _repair_failed(state: GateState, event: MachineEvent[GateEvent]) -> bool:
    return not _repair_succeeded(state, event)


def _diagnosis_changes(state: GateState, event: MachineEvent[GateEvent], correct: bool) -> dict[str, Any]:
    label = _payload(event).get("label", state.diagnosis_label)
    return {
        "diagnosis_attempts": _attempts(state, event),
        "diagnosis_correct": correct,
        "diagnosis_label": label,
        "first_attempt_correct": _first_attempt(state, event, correct),
    }


def _correct_no_fault_update(state: GateState, event: MachineEvent[GateEvent]) -> GateState:
    return _advance(
        state,
        GatePhase.CLEARED,
        **_diagnosis_changes(state, event, True),
        is_no_fault=True,
        receipt_issued=True,
        repair_status=None,
    )


def _correct_fault_update(state: GateState, event: MachineEvent[GateEvent]) -> GateState:
    return _advance(
        state,
        GatePhase.APPROVED,
        **_diagnosis_changes(state, event, True),
        is_no_fault=False,
        receipt_issued=True,
        repair_status=None,
    )


def _unapproved_update(state: GateState, event: MachineEvent[GateEvent]) -> GateState:
    return _advance(
        state,
        GatePhase.AWAIT_DIAGNOSIS,
        **_diagnosis_changes(state, event, True),
        is_no_fault=_no_fault(state, event),
        receipt_issued=False,
        repair_name=None,
        repair_status=None,
    )


def _wrong_update(state: GateState, event: MachineEvent[GateEvent]) -> GateState:
    return _advance(
        state,
        GatePhase.AWAIT_DIAGNOSIS,
        **_diagnosis_changes(state, event, False),
        receipt_issued=False,
        repair_name=None,
        repair_status=None,
    )


def _wrong_cap_update(state: GateState, event: MachineEvent[GateEvent]) -> GateState:
    return _advance(
        state,
        GatePhase.ESCALATED,
        GateEndReason.DIAGNOSIS_ATTEMPTS_EXHAUSTED.value,
        **_diagnosis_changes(state, event, False),
        receipt_issued=False,
        repair_name=None,
        repair_status=None,
    )


def _nudge_update(state: GateState, _event: MachineEvent[GateEvent]) -> GateState:
    return replace(state, nudge_count=state.nudge_count + 1)


def _escalate_update(state: GateState, event: MachineEvent[GateEvent]) -> GateState:
    return _advance(state, GatePhase.ESCALATED, str(_payload(event).get("reason") or "escalated"))


def _budget_update(state: GateState, _event: MachineEvent[GateEvent]) -> GateState:
    return _advance(state, GatePhase.ESCALATED, GateEndReason.BUDGET_TOKENS.value)


def _abort_update(state: GateState, event: MachineEvent[GateEvent]) -> GateState:
    reason = _payload(event).get("reason")
    if reason is None and _payload(event).get("actor") == "operator":
        reason = GateEndReason.ABANDONED.value
    return _advance(state, GatePhase.ESCALATED, str(reason or "aborted"))


def _unknown_effect_update(state: GateState, _event: MachineEvent[GateEvent]) -> GateState:
    return _advance(state, GatePhase.ESCALATED, GateEndReason.UNKNOWN_EFFECT.value)


def _violation_update(violation: GateViolation):
    def update(state: GateState, _event: MachineEvent[GateEvent]) -> GateState:
        return replace(state, violations=state.violations + (violation,))

    return update


def _repair_proposed_update(violation: GateViolation | None):
    def update(state: GateState, event: MachineEvent[GateEvent]) -> GateState:
        violations = state.violations + ((violation,) if violation is not None else ())
        name = _payload(event).get("canonical_name", state.repair_name)
        return replace(state, violations=violations, repair_proposed=True, repair_name=name)

    return update


def _repair_finished_update(success: bool):
    def update(state: GateState, event: MachineEvent[GateEvent]) -> GateState:
        status = str(_payload(event).get("status") or ("success" if success else "failed"))
        if not success:
            return replace(state, repair_status=status)
        return _advance(state, GatePhase.REPAIRED, repair_status=status)

    return update


def _rejections(phases: tuple[GatePhase, ...], reason: str = "terminal") -> dict:
    return {(phase, event): Rejection(reason) for phase in phases for event in GateEvent}


GATE_RULES: dict[tuple[GatePhase, GateEvent], Any] = {
    (GatePhase.AWAIT_DIAGNOSIS, GateEvent.DIAGNOSIS_SUBMITTED): Guards(
        (
            Branch(
                lambda state, event: _correct_and_approved(state, event) and _no_fault(state, event),
                Transition(
                    GateState(phase=GatePhase.CLEARED),
                    (_effect(EffectKind.RECORD_ATTEMPT), _effect(EffectKind.ISSUE_RECEIPT)),
                    update=_correct_no_fault_update,
                ),
            ),
            Branch(
                _correct_and_approved,
                Transition(
                    GateState(phase=GatePhase.APPROVED),
                    (_effect(EffectKind.RECORD_ATTEMPT), _effect(EffectKind.ISSUE_RECEIPT)),
                    update=_correct_fault_update,
                ),
            ),
            Branch(
                _correct_not_approved,
                Transition(
                    GateState(),
                    (_effect(EffectKind.RECORD_ATTEMPT),),
                    update=_unapproved_update,
                ),
            ),
            Branch(
                _wrong_below_cap,
                Transition(GateState(), (_effect(EffectKind.RECORD_ATTEMPT),), update=_wrong_update),
            ),
            Branch(
                _wrong_at_cap,
                Transition(
                    GateState(phase=GatePhase.ESCALATED),
                    (_effect(EffectKind.RECORD_ATTEMPT), _effect(EffectKind.SET_END_REASON)),
                    update=_wrong_cap_update,
                ),
            ),
        )
    ),
    (GatePhase.AWAIT_DIAGNOSIS, GateEvent.NUDGE): Guards(
        (
            Branch(
                lambda state, _event: state.nudge_count < MAX_NUDGES,
                Transition(GateState(), (_effect(EffectKind.EMIT_NUDGE),), update=_nudge_update),
            ),
        ),
        Rejection("nudge cap reached"),
    ),
    (GatePhase.AWAIT_DIAGNOSIS, GateEvent.BUDGET_EXHAUSTED): Transition(
        GateState(phase=GatePhase.ESCALATED),
        (_effect(EffectKind.SET_END_REASON),),
        update=_budget_update,
    ),
    (GatePhase.AWAIT_DIAGNOSIS, GateEvent.REPAIR_PROPOSED): Transition(
        GateState(),
        (_effect(EffectKind.RECORD_VIOLATION, violation=GateViolation.PREMATURE_REPAIR.value),),
        update=_violation_update(GateViolation.PREMATURE_REPAIR),
    ),
    (GatePhase.AWAIT_DIAGNOSIS, GateEvent.ESCALATE): Transition(
        GateState(phase=GatePhase.ESCALATED),
        (_effect(EffectKind.SET_END_REASON),),
        update=_escalate_update,
    ),
    (GatePhase.AWAIT_DIAGNOSIS, GateEvent.ABORT): Transition(
        GateState(phase=GatePhase.ESCALATED),
        (_effect(EffectKind.SET_END_REASON),),
        update=_abort_update,
    ),
    (GatePhase.AWAIT_DIAGNOSIS, GateEvent.REPAIR_FINISHED): Rejection("no repair before diagnosis"),
    (GatePhase.AWAIT_DIAGNOSIS, GateEvent.UNKNOWN_EFFECT): Rejection("no tool before diagnosis"),
    (GatePhase.APPROVED, GateEvent.REPAIR_PROPOSED): Guards(
        (
            Branch(
                _forbidden,
                Transition(
                    GateState(phase=GatePhase.APPROVED),
                    (_effect(EffectKind.RECORD_VIOLATION, violation=GateViolation.FORBIDDEN_REPAIR.value),),
                    update=_repair_proposed_update(GateViolation.FORBIDDEN_REPAIR),
                ),
            ),
            Branch(
                lambda _state, event: not bool(_payload(event).get("acceptable")),
                Transition(
                    GateState(phase=GatePhase.APPROVED),
                    (_effect(EffectKind.RECORD_VIOLATION, violation=GateViolation.UNACCEPTABLE_REPAIR.value),),
                    update=_repair_proposed_update(GateViolation.UNACCEPTABLE_REPAIR),
                ),
            ),
            Branch(
                lambda _state, event: bool(_payload(event).get("acceptable")),
                Transition(
                    GateState(phase=GatePhase.APPROVED),
                    (_effect(EffectKind.SET_PROJECTION, repair_proposed=True),),
                    update=_repair_proposed_update(None),
                ),
            ),
        )
    ),
    (GatePhase.APPROVED, GateEvent.REPAIR_FINISHED): Guards(
        (
            Branch(
                _repair_succeeded,
                Transition(
                    GateState(phase=GatePhase.REPAIRED),
                    (_effect(EffectKind.SET_PROJECTION, repair_status="success"),),
                    update=_repair_finished_update(True),
                ),
            ),
            Branch(
                _repair_failed,
                Transition(
                    GateState(phase=GatePhase.APPROVED),
                    (_effect(EffectKind.SET_PROJECTION, repair_status="failure"),),
                    update=_repair_finished_update(False),
                ),
            ),
        )
    ),
    (GatePhase.APPROVED, GateEvent.UNKNOWN_EFFECT): Transition(
        GateState(phase=GatePhase.ESCALATED),
        (
            _effect(EffectKind.SET_END_REASON),
            _effect(EffectKind.ABORT_AND_RETRY),
        ),
        update=_unknown_effect_update,
    ),
    (GatePhase.APPROVED, GateEvent.ESCALATE): Transition(
        GateState(phase=GatePhase.ESCALATED),
        (_effect(EffectKind.SET_END_REASON),),
        update=_escalate_update,
    ),
    (GatePhase.APPROVED, GateEvent.BUDGET_EXHAUSTED): Transition(
        GateState(phase=GatePhase.ESCALATED),
        (_effect(EffectKind.SET_END_REASON),),
        update=_budget_update,
    ),
    (GatePhase.APPROVED, GateEvent.ABORT): Transition(
        GateState(phase=GatePhase.ESCALATED),
        (_effect(EffectKind.SET_END_REASON),),
        update=_abort_update,
    ),
    (GatePhase.APPROVED, GateEvent.DIAGNOSIS_SUBMITTED): Guards(
        (
            Branch(
                lambda state, event: _correct_and_approved(state, event),
                Transition(
                    GateState(phase=GatePhase.APPROVED),
                    (_effect(EffectKind.RECORD_ATTEMPT), _effect(EffectKind.ISSUE_RECEIPT)),
                    update=_correct_fault_update,
                ),
            ),
            Branch(
                _correct_not_approved,
                Transition(
                    GateState(),
                    (_effect(EffectKind.RECORD_ATTEMPT),),
                    update=_unapproved_update,
                ),
            ),
            Branch(
                _wrong_below_cap,
                Transition(
                    GateState(),
                    (_effect(EffectKind.RECORD_ATTEMPT),),
                    update=_wrong_update,
                ),
            ),
            Branch(
                _wrong_at_cap,
                Transition(
                    GateState(phase=GatePhase.ESCALATED),
                    (_effect(EffectKind.RECORD_ATTEMPT), _effect(EffectKind.SET_END_REASON)),
                    update=_wrong_cap_update,
                ),
            ),
        )
    ),
    (GatePhase.APPROVED, GateEvent.NUDGE): Rejection("nudge only before diagnosis"),
    **_rejections((GatePhase.REPAIRED, GatePhase.ESCALATED)),
    **_rejections((GatePhase.CLEARED,), "repair after no fault is the only cleared transition"),
    (GatePhase.CLEARED, GateEvent.REPAIR_PROPOSED): Transition(
        GateState(phase=GatePhase.CLEARED),
        (_effect(EffectKind.RECORD_VIOLATION, violation=GateViolation.REPAIR_AFTER_NO_FAULT.value),),
        update=_violation_update(GateViolation.REPAIR_AFTER_NO_FAULT),
    ),
}

GATE_MACHINE = Machine(
    kind=MachineKind.GATE,
    states=GATE_STATES,
    events=tuple(GateEvent),
    initial=GateState(),
    rules=GATE_RULES,
    state_id=lambda state: state.phase,
)


def select_terminal_event(events: list[MachineEvent[GateEvent]]) -> MachineEvent[GateEvent] | None:
    for event_type in TERMINAL_PRECEDENCE:
        for event in events:
            if event.type == event_type:
                return event
    return None
