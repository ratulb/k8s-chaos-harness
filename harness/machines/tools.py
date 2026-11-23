from __future__ import annotations

from dataclasses import dataclass, fields
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


class ToolPhase(StrEnum):
    INTENDED = "intended"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    UNKNOWN_EFFECT = "unknown_effect"
    SUPERSEDED = "superseded"


class ToolEvent(StrEnum):
    PREPARE = "prepare"
    START = "start"
    SUCCESS = "success"
    FAILURE = "failure"
    DENY = "deny"
    CANCEL = "cancel"
    LEASE_EXPIRED = "lease_expired"
    RESUME_SUPERSEDE = "resume_supersede"


class ToolCrashAction(StrEnum):
    RESOLVE_OUTCOME = "resolve_outcome"
    NONE = "none"


class ToolResultCode(StrEnum):
    SUCCESS = "success"
    DENIED = "denied"
    UNKNOWN_EFFECT = "unknown_effect"
    SUPERSEDED = "superseded_after_resume"


TOOL_PROJECTION = {
    ToolPhase.INTENDED: ("proposed", None),
    ToolPhase.STARTED: ("started", None),
    ToolPhase.SUCCEEDED: ("finished", ToolResultCode.SUCCESS.value),
    ToolPhase.FAILED: ("failed", None),
    ToolPhase.DENIED: ("denied", ToolResultCode.DENIED.value),
    ToolPhase.UNKNOWN_EFFECT: ("failed", ToolResultCode.UNKNOWN_EFFECT.value),
    ToolPhase.SUPERSEDED: ("finished", ToolResultCode.SUPERSEDED.value),
}

TERMINAL_TOOL_PHASES = {
    ToolPhase.SUCCEEDED,
    ToolPhase.FAILED,
    ToolPhase.DENIED,
    ToolPhase.UNKNOWN_EFFECT,
    ToolPhase.SUPERSEDED,
}

TOOL_STATES = {
    phase: StateSpec(
        value=status,
        rank=rank,
        terminal=phase in TERMINAL_TOOL_PHASES,
        resumable=phase not in TERMINAL_TOOL_PHASES,
        run_terminal=False,
        end_reason_required=False,
        crash_action=ToolCrashAction.NONE
        if phase in TERMINAL_TOOL_PHASES
        else ToolCrashAction.RESOLVE_OUTCOME,
    )
    for rank, (phase, (status, _code)) in enumerate(TOOL_PROJECTION.items())
}


@dataclass(frozen=True)
class ToolState:
    phase: ToolPhase = ToolPhase.INTENDED
    persisted_status: str = "proposed"
    result_code: str | None = None
    mutating: bool = False
    destructive: bool = False
    reason: str | None = None
    violation_kind: str | None = None
    visited: tuple[ToolPhase, ...] = (ToolPhase.INTENDED,)


def _effect(kind: EffectKind, **payload: Any) -> Effect:
    return Effect(kind, payload)


def _advance(
    state: ToolState,
    phase: ToolPhase,
    reason: str | None = None,
    **changes: Any,
) -> ToolState:
    status, code = TOOL_PROJECTION[phase]
    values = {field.name: getattr(state, field.name) for field in fields(state)}
    values["phase"] = phase
    values["persisted_status"] = status
    values["result_code"] = changes.pop("result_code", code)
    if reason is not None:
        values["reason"] = reason
    if phase != state.phase:
        values["visited"] = state.visited + (phase,)
    values.update(changes)
    return ToolState(**values)


def _payload(event: MachineEvent[ToolEvent]) -> dict[str, Any]:
    return event.payload


def _prepare_update(state: ToolState, event: MachineEvent[ToolEvent]) -> ToolState:
    return replace_tool(
        state,
        mutating=bool(_payload(event).get("mutating")),
        destructive=bool(_payload(event).get("destructive")),
    )


def _failure_update(state: ToolState, event: MachineEvent[ToolEvent]) -> ToolState:
    code = str(_payload(event).get("code") or "failed")
    return _advance(state, ToolPhase.FAILED, result_code=code)


def _deny_update(state: ToolState, event: MachineEvent[ToolEvent]) -> ToolState:
    return _advance(
        state,
        ToolPhase.DENIED,
        reason=str(_payload(event).get("reason") or "denied"),
        violation_kind=_payload(event).get("violation_kind"),
    )


def _unknown_effect_update(state: ToolState, _event: MachineEvent[ToolEvent]) -> ToolState:
    return _advance(
        state,
        ToolPhase.UNKNOWN_EFFECT,
        reason="mutating tool outcome unknown",
    )


def _superseded_update(state: ToolState, event: MachineEvent[ToolEvent]) -> ToolState:
    return _advance(
        state,
        ToolPhase.SUPERSEDED,
        reason=str(_payload(event).get("reason") or "superseded_after_resume"),
    )


def replace_tool(state: ToolState, **changes: Any) -> ToolState:
    values = {field.name: getattr(state, field.name) for field in fields(state)}
    values.update(changes)
    return ToolState(**values)


def _is_mutating(state: ToolState, _event: MachineEvent[ToolEvent]) -> bool:
    return state.mutating


def _is_read_only(state: ToolState, _event: MachineEvent[ToolEvent]) -> bool:
    return not state.mutating


def _rejections(phases: tuple[ToolPhase, ...], reason: str) -> dict:
    return {(phase, event): Rejection(reason) for phase in phases for event in ToolEvent}


TOOL_RULES: dict[tuple[ToolPhase, ToolEvent], Any] = {}
TOOL_RULES.update(_rejections(tuple(ToolPhase), "terminal"))
TOOL_RULES.update(
    {
        (ToolPhase.INTENDED, ToolEvent.PREPARE): Transition(
            ToolState(),
            (_effect(EffectKind.SET_PROJECTION, intent=True),),
            update=_prepare_update,
        ),
        (ToolPhase.INTENDED, ToolEvent.START): Transition(
            ToolState(phase=ToolPhase.STARTED),
            (_effect(EffectKind.SET_PROJECTION, started=True),),
            update=lambda state, _event: _advance(state, ToolPhase.STARTED),
        ),
        (ToolPhase.INTENDED, ToolEvent.DENY): Transition(
            ToolState(phase=ToolPhase.DENIED),
            (
                _effect(EffectKind.RECORD_VIOLATION),
                _effect(EffectKind.SET_PROJECTION, denied=True),
            ),
            update=_deny_update,
        ),
        (ToolPhase.STARTED, ToolEvent.SUCCESS): Transition(
            ToolState(phase=ToolPhase.SUCCEEDED),
            (_effect(EffectKind.SET_PROJECTION, result_code=ToolResultCode.SUCCESS.value),),
            update=lambda state, _event: _advance(state, ToolPhase.SUCCEEDED),
        ),
        (ToolPhase.STARTED, ToolEvent.FAILURE): Transition(
            ToolState(phase=ToolPhase.FAILED),
            (_effect(EffectKind.SET_PROJECTION, result_code="failed"),),
            update=_failure_update,
        ),
        (ToolPhase.STARTED, ToolEvent.CANCEL): Guards(
            (
                Branch(
                    _is_mutating,
                    Transition(
                        ToolState(phase=ToolPhase.UNKNOWN_EFFECT),
                        (
                            _effect(EffectKind.SET_PROJECTION, result_code=ToolResultCode.UNKNOWN_EFFECT.value),
                            _effect(EffectKind.ABORT_AND_RETRY),
                        ),
                        update=_unknown_effect_update,
                    ),
                ),
            ),
            Rejection("read-only cancel after start"),
        ),
        (ToolPhase.STARTED, ToolEvent.LEASE_EXPIRED): Guards(
            (
                Branch(
                    _is_mutating,
                    Transition(
                        ToolState(phase=ToolPhase.UNKNOWN_EFFECT),
                        (
                            _effect(EffectKind.SET_PROJECTION, result_code=ToolResultCode.UNKNOWN_EFFECT.value),
                            _effect(EffectKind.ABORT_AND_RETRY),
                        ),
                        update=_unknown_effect_update,
                    ),
                ),
                Branch(
                    _is_read_only,
                    Transition(
                        ToolState(phase=ToolPhase.SUPERSEDED),
                        (_effect(EffectKind.SUPERSEDE_TOOL_CALLS),),
                        update=_superseded_update,
                    ),
                ),
            )
        ),
    }
)

for phase in (ToolPhase.INTENDED, ToolPhase.STARTED):
    TOOL_RULES[(phase, ToolEvent.RESUME_SUPERSEDE)] = Guards(
        (
            Branch(
                _is_read_only,
                Transition(
                    ToolState(phase=ToolPhase.SUPERSEDED),
                    (_effect(EffectKind.SUPERSEDE_TOOL_CALLS),),
                    update=_superseded_update,
                ),
            ),
        ),
        Rejection("mutating tool cannot be superseded"),
    )

TOOL_MACHINE = Machine(
    kind=MachineKind.TOOL,
    states=TOOL_STATES,
    events=tuple(ToolEvent),
    initial=ToolState(),
    rules=TOOL_RULES,
    state_id=lambda state: state.phase,
)
