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


class RunPhase(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    RESETTING = "resetting"
    BASELINING = "baselining"
    INJECTING = "injecting"
    FAULT_CHECK = "fault_check"
    AGENT_RUNNING = "agent_running"
    VERIFYING = "verifying"
    INTERRUPTED = "interrupted"
    FINISHED = "finished"
    ABORTED = "aborted"
    SKIPPED_UNSUPPORTED = "skipped_unsupported"


class RunEvent(StrEnum):
    CREATE = "create"
    LEASE = "lease"
    SKIP_UNSUPPORTED = "skip_unsupported"
    BEGIN_RESET = "begin_reset"
    RESET_OK = "reset_ok"
    RESET_FAILED = "reset_failed"
    BASELINE_OK = "baseline_ok"
    BASELINE_FAILED = "baseline_failed"
    INJECT_OK = "inject_ok"
    INJECT_FAILED = "inject_failed"
    FAULT_OBSERVED = "fault_observed"
    FAULT_NOT_OBSERVED = "fault_not_observed"
    AGENT_DONE = "agent_done"
    AGENT_TERMINAL = "agent_terminal"
    VERIFY_OK = "verify_ok"
    VERIFY_FAILED = "verify_failed"
    ABORT = "abort"
    ABANDON = "abandon"
    PROVIDER_ERROR_ALL = "provider_error_all"
    ILLEGAL_TRANSITION = "illegal_transition"
    LEASE_EXPIRED = "lease_expired"
    REAP = "reap"


class RunCrashAction(StrEnum):
    RESUME_RELEASE = "resume_re_lease"
    NEW_RUN_AFTER_RESET = "new_run_after_reset"
    RESUME = "resume"
    ABORT_AND_RETRY = "abort_and_retry"
    RERUN_VERIFY = "rerun_verify"
    NONE = "none"


class RunEndReason(StrEnum):
    UNSUPPORTED = "unsupported"
    RESET_FAILED = "reset_failed"
    BASELINE_FAILED = "baseline_failed"
    INJECTION_FAILED = "injection_failed"
    FAULT_NOT_OBSERVED = "fault_not_observed"
    PROVIDER_ERROR_ALL = "provider_error_all"
    VERIFY_OK = "verify_ok"
    VERIFY_FAILED = "verify_failed"
    STATE_MACHINE_BUG = "state_machine_bug"
    LEASE_EXPIRED = "lease_expired"
    UNKNOWN_PREVIOUS_STATE = "interrupted_unknown_previous_state"
    ABANDONED = "abandoned"


RUN_PROJECTION = {
    RunPhase.PENDING: ("pending", "queued"),
    RunPhase.LEASED: ("running", "leased"),
    RunPhase.RESETTING: ("running", "resetting"),
    RunPhase.BASELINING: ("running", "baseline"),
    RunPhase.INJECTING: ("running", "injecting"),
    RunPhase.FAULT_CHECK: ("running", "fault_check"),
    RunPhase.AGENT_RUNNING: ("running", "agent_running"),
    RunPhase.VERIFYING: ("running", "verifying"),
    RunPhase.INTERRUPTED: ("interrupted", "interrupted"),
    RunPhase.FINISHED: ("finished", "finished"),
    RunPhase.ABORTED: ("aborted", "aborted"),
    RunPhase.SKIPPED_UNSUPPORTED: ("skipped_unsupported", "skipped_unsupported"),
}


RUN_STATES = {
    phase: StateSpec(
        value=f"{status}:{wire_phase}",
        rank=rank,
        terminal=phase in {
            RunPhase.FINISHED,
            RunPhase.ABORTED,
            RunPhase.SKIPPED_UNSUPPORTED,
        },
        resumable=phase not in {
            RunPhase.FINISHED,
            RunPhase.ABORTED,
            RunPhase.SKIPPED_UNSUPPORTED,
        },
        run_terminal=phase in {
            RunPhase.FINISHED,
            RunPhase.ABORTED,
            RunPhase.SKIPPED_UNSUPPORTED,
        },
        end_reason_required=phase in {
            RunPhase.INTERRUPTED,
            RunPhase.FINISHED,
            RunPhase.ABORTED,
            RunPhase.SKIPPED_UNSUPPORTED,
        },
        crash_action={
            RunPhase.PENDING: RunCrashAction.RESUME_RELEASE,
            RunPhase.LEASED: RunCrashAction.RESUME_RELEASE,
            RunPhase.RESETTING: RunCrashAction.RESUME_RELEASE,
            RunPhase.BASELINING: RunCrashAction.RESUME_RELEASE,
            RunPhase.INJECTING: RunCrashAction.NEW_RUN_AFTER_RESET,
            RunPhase.FAULT_CHECK: RunCrashAction.NEW_RUN_AFTER_RESET,
            RunPhase.AGENT_RUNNING: RunCrashAction.RESUME,
            RunPhase.VERIFYING: RunCrashAction.RERUN_VERIFY,
            RunPhase.INTERRUPTED: RunCrashAction.RESUME,
        }.get(phase, RunCrashAction.NONE),
        renew_lease=phase in {
            RunPhase.LEASED,
            RunPhase.RESETTING,
            RunPhase.BASELINING,
            RunPhase.INJECTING,
            RunPhase.FAULT_CHECK,
            RunPhase.AGENT_RUNNING,
            RunPhase.VERIFYING,
        },
    )
    for rank, (phase, (status, wire_phase)) in enumerate(RUN_PROJECTION.items())
}


CRASH_ACTIONS = {
    RunPhase.PENDING: RunCrashAction.RESUME_RELEASE,
    RunPhase.LEASED: RunCrashAction.RESUME_RELEASE,
    RunPhase.RESETTING: RunCrashAction.RESUME_RELEASE,
    RunPhase.BASELINING: RunCrashAction.RESUME_RELEASE,
    RunPhase.INJECTING: RunCrashAction.NEW_RUN_AFTER_RESET,
    RunPhase.FAULT_CHECK: RunCrashAction.NEW_RUN_AFTER_RESET,
    RunPhase.AGENT_RUNNING: RunCrashAction.RESUME,
    RunPhase.VERIFYING: RunCrashAction.RERUN_VERIFY,
}


@dataclass(frozen=True)
class RunState:
    phase: RunPhase = RunPhase.PENDING
    status: str = "pending"
    wire_phase: str = "queued"
    previous_state: RunPhase | None = None
    end_reason: str | None = None
    idempotency_key: str | None = None
    lease_owner: str | None = None
    lease_expires_at: float | None = None
    fault_present: bool = False
    gate_terminal: str | None = None
    gate_reason: str | None = None
    verify_ok: bool | None = None
    attempts: tuple[Any, ...] = ()
    transition_error: str | None = None
    visited: tuple[RunPhase, ...] = (RunPhase.PENDING,)


NONTERMINAL = tuple(phase for phase, spec in RUN_STATES.items() if not spec.terminal)
TERMINAL = tuple(phase for phase, spec in RUN_STATES.items() if spec.terminal)
PARKED_RESUMABLE = (RunPhase.INTERRUPTED,)


def _effect(kind: EffectKind, **payload: Any) -> Effect:
    return Effect(kind, payload)


def _advance(
    state: RunState,
    phase: RunPhase,
    reason: str | None = None,
    **changes: Any,
) -> RunState:
    status, wire_phase = RUN_PROJECTION[phase]
    values = {field.name: getattr(state, field.name) for field in fields(state)}
    values["phase"] = phase
    values["status"] = status
    values["wire_phase"] = wire_phase
    if reason is not None:
        values["end_reason"] = reason
    if phase != state.phase:
        values["visited"] = state.visited + (phase,)
    values.update(changes)
    return RunState(**values)


def _payload(event: MachineEvent[RunEvent]) -> dict[str, Any]:
    return event.payload


def _to_phase(phase: RunPhase, reason: str | None = None, **changes: Any):
    def update(state: RunState, _event: MachineEvent[RunEvent]) -> RunState:
        return _advance(state, phase, reason, **changes)

    return update


def _lease_update(state: RunState, event: MachineEvent[RunEvent]) -> RunState:
    return _advance(
        state,
        RunPhase.LEASED,
        lease_owner=str(_payload(event).get("lease_owner") or "unknown"),
        lease_expires_at=_payload(event).get("lease_expires_at"),
    )


def _abort_update(state: RunState, event: MachineEvent[RunEvent]) -> RunState:
    reason = str(_payload(event).get("reason") or "aborted")
    return _advance(state, RunPhase.ABORTED, reason, idempotency_key=state.idempotency_key)


def _abandon_update(state: RunState, _event: MachineEvent[RunEvent]) -> RunState:
    return _advance(state, RunPhase.ABORTED, RunEndReason.ABANDONED.value)


def _illegal_update(state: RunState, event: MachineEvent[RunEvent]) -> RunState:
    detail = f"{_payload(event).get('machine', '?')}:{_payload(event).get('event', '?')}"
    return _advance(
        state,
        RunPhase.ABORTED,
        RunEndReason.STATE_MACHINE_BUG.value,
        transition_error=str(_payload(event).get("error") or detail),
    )


def _terminal_reason_update(phase: RunPhase, reason: RunEndReason):
    def update(state: RunState, _event: MachineEvent[RunEvent]) -> RunState:
        return _advance(state, phase, reason.value)

    return update


def _agent_done_update(state: RunState, event: MachineEvent[RunEvent]) -> RunState:
    return _advance(
        state,
        RunPhase.VERIFYING,
        gate_terminal=str(_payload(event).get("gate_terminal") or ""),
        gate_reason=_payload(event).get("gate_reason"),
    )


def _agent_terminal_update(state: RunState, event: MachineEvent[RunEvent]) -> RunState:
    return _advance(state, RunPhase.ABORTED, str(_payload(event).get("reason") or "gate_escalation"))


def _provider_error_update(state: RunState, event: MachineEvent[RunEvent]) -> RunState:
    return _advance(
        state,
        RunPhase.ABORTED,
        RunEndReason.PROVIDER_ERROR_ALL.value,
        attempts=tuple(_payload(event).get("attempts", ())),
    )


def _verify_update(success: bool):
    def update(state: RunState, _event: MachineEvent[RunEvent]) -> RunState:
        reason = RunEndReason.VERIFY_OK if success else RunEndReason.VERIFY_FAILED
        return _advance(state, RunPhase.FINISHED, reason.value, verify_ok=success)

    return update


def _previous_state(event: MachineEvent[RunEvent]) -> RunPhase | None:
    value = _payload(event).get("previous_state") or _payload(event).get("from_state")
    if value is None:
        return None
    try:
        return RunPhase(value)
    except ValueError:
        return None


def _has_previous(_state: RunState, event: MachineEvent[RunEvent]) -> bool:
    return _previous_state(event) is not None


def _same_key(state: RunState, event: MachineEvent[RunEvent]) -> bool:
    return state.idempotency_key is not None and _payload(event).get("idempotency_key") == state.idempotency_key


def _interrupt_update(state: RunState, event: MachineEvent[RunEvent]) -> RunState:
    previous = _previous_state(event) or state.phase
    key = str(
        _payload(event).get("idempotency_key")
        or f"reap:{state.phase.value}:{_payload(event).get('lease_expires_at')}"
    )
    return _advance(
        state,
        RunPhase.INTERRUPTED,
        RunEndReason.LEASE_EXPIRED.value,
        previous_state=previous,
        idempotency_key=key,
        lease_expires_at=_payload(event).get("lease_expires_at"),
    )


def _unknown_previous_update(state: RunState, _event: MachineEvent[RunEvent]) -> RunState:
    return _advance(state, RunPhase.ABORTED, RunEndReason.UNKNOWN_PREVIOUS_STATE.value)


def _no_op_update(state: RunState, _event: MachineEvent[RunEvent]) -> RunState:
    return state


def _rejections(phases: tuple[RunPhase, ...], reason: str) -> dict:
    return {(phase, event): Rejection(reason) for phase in phases for event in RunEvent}


RUN_RULES: dict[tuple[RunPhase, RunEvent], Any] = {}
RUN_RULES.update(_rejections(tuple(RunPhase), "terminal"))
RUN_RULES.update(
    {
        (RunPhase.PENDING, RunEvent.CREATE): Transition(
            RunState(),
            (_effect(EffectKind.SET_PROJECTION, created=True),),
        ),
        (RunPhase.PENDING, RunEvent.LEASE): Transition(
            RunState(phase=RunPhase.LEASED),
            (_effect(EffectKind.ACQUIRE_LEASE),),
            update=_lease_update,
        ),
    }
)

for skip_phase in (RunPhase.PENDING, RunPhase.LEASED):
    RUN_RULES[(skip_phase, RunEvent.SKIP_UNSUPPORTED)] = Transition(
        RunState(phase=RunPhase.SKIPPED_UNSUPPORTED),
        (
            _effect(EffectKind.SET_END_REASON),
            _effect(EffectKind.SET_PROJECTION, terminal_record=True),
        ),
        update=_terminal_reason_update(RunPhase.SKIPPED_UNSUPPORTED, RunEndReason.UNSUPPORTED),
    )

RUN_RULES.update(
    {
        (RunPhase.LEASED, RunEvent.BEGIN_RESET): Transition(
            RunState(phase=RunPhase.RESETTING),
            (_effect(EffectKind.RENEW_LEASE),),
            update=_to_phase(RunPhase.RESETTING),
        ),
        (RunPhase.RESETTING, RunEvent.RESET_OK): Transition(
            RunState(phase=RunPhase.BASELINING),
            (_effect(EffectKind.RENEW_LEASE),),
            update=_to_phase(RunPhase.BASELINING),
        ),
        (RunPhase.RESETTING, RunEvent.RESET_FAILED): Transition(
            RunState(phase=RunPhase.ABORTED),
            (_effect(EffectKind.RELEASE_LEASE),),
            update=_terminal_reason_update(RunPhase.ABORTED, RunEndReason.RESET_FAILED),
        ),
        (RunPhase.BASELINING, RunEvent.BASELINE_OK): Transition(
            RunState(phase=RunPhase.INJECTING),
            (_effect(EffectKind.RENEW_LEASE),),
            update=_to_phase(RunPhase.INJECTING),
        ),
        (RunPhase.BASELINING, RunEvent.BASELINE_FAILED): Transition(
            RunState(phase=RunPhase.ABORTED),
            (_effect(EffectKind.RELEASE_LEASE),),
            update=_terminal_reason_update(RunPhase.ABORTED, RunEndReason.BASELINE_FAILED),
        ),
        (RunPhase.INJECTING, RunEvent.INJECT_OK): Transition(
            RunState(phase=RunPhase.FAULT_CHECK),
            (_effect(EffectKind.RENEW_LEASE),),
            update=_to_phase(RunPhase.FAULT_CHECK),
        ),
        (RunPhase.INJECTING, RunEvent.INJECT_FAILED): Transition(
            RunState(phase=RunPhase.ABORTED),
            (_effect(EffectKind.RELEASE_LEASE),),
            update=_terminal_reason_update(RunPhase.ABORTED, RunEndReason.INJECTION_FAILED),
        ),
        (RunPhase.FAULT_CHECK, RunEvent.FAULT_OBSERVED): Transition(
            RunState(phase=RunPhase.AGENT_RUNNING),
            (_effect(EffectKind.SET_PROJECTION, fault_present=True),),
            update=_to_phase(RunPhase.AGENT_RUNNING, fault_present=True),
        ),
        (RunPhase.FAULT_CHECK, RunEvent.FAULT_NOT_OBSERVED): Transition(
            RunState(phase=RunPhase.ABORTED),
            (_effect(EffectKind.RELEASE_LEASE),),
            update=_terminal_reason_update(RunPhase.ABORTED, RunEndReason.FAULT_NOT_OBSERVED),
        ),
        (RunPhase.AGENT_RUNNING, RunEvent.AGENT_DONE): Transition(
            RunState(phase=RunPhase.VERIFYING),
            (_effect(EffectKind.SCORE_PATH),),
            update=_agent_done_update,
        ),
        (RunPhase.AGENT_RUNNING, RunEvent.AGENT_TERMINAL): Transition(
            RunState(phase=RunPhase.ABORTED),
            (_effect(EffectKind.RELEASE_LEASE),),
            update=_agent_terminal_update,
        ),
        (RunPhase.AGENT_RUNNING, RunEvent.PROVIDER_ERROR_ALL): Transition(
            RunState(phase=RunPhase.ABORTED),
            (_effect(EffectKind.RELEASE_LEASE),),
            update=_provider_error_update,
        ),
        (RunPhase.VERIFYING, RunEvent.VERIFY_OK): Transition(
            RunState(phase=RunPhase.FINISHED),
            (
                _effect(EffectKind.SET_PROJECTION, verify_ok=True),
                _effect(EffectKind.RELEASE_LEASE),
            ),
            update=_verify_update(True),
        ),
        (RunPhase.VERIFYING, RunEvent.VERIFY_FAILED): Transition(
            RunState(phase=RunPhase.FINISHED),
            (
                _effect(EffectKind.SET_PROJECTION, verify_ok=False),
                _effect(EffectKind.RELEASE_LEASE),
            ),
            update=_verify_update(False),
        ),
        (RunPhase.INTERRUPTED, RunEvent.ABANDON): Transition(
            RunState(phase=RunPhase.ABORTED),
            (_effect(EffectKind.RELEASE_LEASE),),
            update=_abandon_update,
        ),
    }
)

for phase in NONTERMINAL:
    RUN_RULES[(phase, RunEvent.ABORT)] = Transition(
        RunState(phase=RunPhase.ABORTED),
        (_effect(EffectKind.RELEASE_LEASE),),
        update=_abort_update,
    )
    RUN_RULES[(phase, RunEvent.ILLEGAL_TRANSITION)] = Transition(
        RunState(phase=RunPhase.ABORTED),
        (
            _effect(EffectKind.APPEND_EVENT, event_type="state_transition_rejected"),
            _effect(EffectKind.RELEASE_LEASE),
        ),
        update=_illegal_update,
    )

for phase in (p for p in NONTERMINAL if p not in PARKED_RESUMABLE):
    interrupt_rule = Guards(
        (
            Branch(_has_previous, Transition(RunState(phase=RunPhase.INTERRUPTED), update=_interrupt_update)),
        ),
        Transition(RunState(phase=RunPhase.ABORTED), update=_unknown_previous_update),
    )
    RUN_RULES[(phase, RunEvent.LEASE_EXPIRED)] = interrupt_rule
    RUN_RULES[(phase, RunEvent.REAP)] = interrupt_rule

for phase in PARKED_RESUMABLE:
    RUN_RULES[(phase, RunEvent.LEASE_EXPIRED)] = Guards(
        (
            Branch(
                _same_key,
                Transition(RunState(phase=RunPhase.INTERRUPTED), update=_no_op_update),
            ),
        ),
        Rejection("leaseExpired idempotency key mismatch"),
    )
    RUN_RULES[(phase, RunEvent.REAP)] = RUN_RULES[(phase, RunEvent.LEASE_EXPIRED)]

RUN_MACHINE = Machine(
    kind=MachineKind.RUN,
    states=RUN_STATES,
    events=tuple(RunEvent),
    initial=RunState(),
    rules=RUN_RULES,
    state_id=lambda state: state.phase,
)


def resume_triage(previous_state: RunPhase | None, unknown_effect: bool = False) -> RunCrashAction:
    if previous_state is None:
        return RunCrashAction.NONE
    if previous_state in {
        RunPhase.PENDING,
        RunPhase.LEASED,
        RunPhase.RESETTING,
        RunPhase.BASELINING,
    }:
        return RunCrashAction.RESUME_RELEASE
    if previous_state in {RunPhase.INJECTING, RunPhase.FAULT_CHECK}:
        return RunCrashAction.NEW_RUN_AFTER_RESET
    if previous_state == RunPhase.AGENT_RUNNING:
        return RunCrashAction.ABORT_AND_RETRY if unknown_effect else RunCrashAction.RESUME
    if previous_state == RunPhase.VERIFYING:
        return RunCrashAction.RERUN_VERIFY
    return RunCrashAction.NONE
