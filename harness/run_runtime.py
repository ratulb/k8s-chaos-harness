from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping

from harness.machines.run import (
    RUN_MACHINE,
    RUN_PROJECTION,
    RUN_STATES,
    RunCrashAction,
    RunEndReason,
    RunEvent,
    RunPhase,
    RunState,
    resume_triage,
)
from harness.state_machine import IllegalTransition, MachineEvent, rejection_event


@dataclass(frozen=True)
class RunFacts:
    phase: RunPhase
    status: str
    wire_phase: str
    end_reason: str | None
    terminal: bool
    resumable: bool


def canonical_run_event(event: MachineEvent[RunEvent]) -> dict[str, Any]:
    payload: dict[str, Any] = {"event": event.type.value, "payload": dict(event.payload)}
    if event.idempotency_key is not None:
        payload["idempotency_key"] = event.idempotency_key
    if event.seq is not None:
        payload["seq"] = event.seq
    return {"type": f"run.{event.type.value}", "payload": payload}


def _event_to_dict(event: MachineEvent[RunEvent]) -> dict[str, Any]:
    data: dict[str, Any] = {"type": event.type.value, "payload": dict(event.payload)}
    if event.idempotency_key is not None:
        data["idempotency_key"] = event.idempotency_key
    if event.seq is not None:
        data["seq"] = event.seq
    return data


def _state_to_dict(state: RunState) -> dict[str, Any]:
    data = asdict(state)
    data["phase"] = state.phase.value
    data["visited"] = [phase.value for phase in state.visited]
    if state.previous_state is not None:
        data["previous_state"] = state.previous_state.value
    return data


def _state_from_dict(data: Mapping[str, Any] | None) -> RunState:
    if not isinstance(data, Mapping):
        return RunState()
    values = dict(data)
    phase = values.get("phase")
    if isinstance(phase, str):
        try:
            values["phase"] = RunPhase(phase)
        except ValueError:
            values.pop("phase", None)
    previous = values.get("previous_state")
    if isinstance(previous, str):
        try:
            values["previous_state"] = RunPhase(previous)
        except ValueError:
            values["previous_state"] = None
    visited = values.get("visited")
    if isinstance(visited, list):
        parsed: list[RunPhase] = []
        for item in visited:
            try:
                parsed.append(RunPhase(str(item)))
            except ValueError:
                continue
        values["visited"] = tuple(parsed) or (RunPhase.PENDING,)
    allowed = {name for name in RunState.__dataclass_fields__}
    return RunState(**{key: value for key, value in values.items() if key in allowed})


@dataclass
class RunRuntime:
    run_id: str
    state: RunState = field(default_factory=RunState)
    events: list[MachineEvent[RunEvent]] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)
    _seen_keys: set[str] = field(default_factory=set, repr=False)
    log: Callable[[dict[str, Any]], int] | None = field(default=None, repr=False)

    @property
    def status(self) -> str:
        return self.state.status

    @property
    def wire_phase(self) -> str:
        return self.state.wire_phase

    @property
    def end_reason(self) -> str | None:
        return self.state.end_reason

    @property
    def fault_present(self) -> bool:
        return self.state.fault_present

    @property
    def verify_ok(self) -> bool | None:
        return self.state.verify_ok

    @property
    def terminal(self) -> bool:
        return RUN_STATES[self.state.phase].terminal

    @property
    def resumable(self) -> bool:
        return RUN_STATES[self.state.phase].resumable

    @property
    def renew_lease(self) -> bool:
        return RUN_STATES[self.state.phase].renew_lease

    @property
    def facts(self) -> RunFacts:
        spec = RUN_STATES[self.state.phase]
        return RunFacts(
            phase=self.state.phase,
            status=self.state.status,
            wire_phase=self.state.wire_phase,
            end_reason=self.state.end_reason,
            terminal=spec.terminal,
            resumable=spec.resumable,
        )

    def projection(self) -> dict[str, Any]:
        return {
            "status": self.state.status,
            "phase": self.state.wire_phase,
            "end_reason": self.state.end_reason,
            "fault_present": self.state.fault_present,
            "verify_ok": self.state.verify_ok,
        }

    def emit(
        self,
        event_type: RunEvent,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        seq: int | None = None,
    ) -> MachineEvent[RunEvent] | None:
        if idempotency_key is not None and idempotency_key in self._seen_keys:
            return None
        event = MachineEvent(event_type, dict(payload or {}), idempotency_key=idempotency_key, seq=seq)
        try:
            transition = RUN_MACHINE.transition(self.state, event)
        except IllegalTransition as exc:
            self.rejections.append(str(exc))
            if self.log is not None:
                self.log(rejection_event(RUN_MACHINE, self.state, event, str(exc)))
            return None
        if idempotency_key is not None:
            self._seen_keys.add(idempotency_key)
        self.events.append(event)
        self.state = transition.state
        if self.log is not None:
            self.log(canonical_run_event(event))
        return event

    def create(self) -> None:
        self.emit(RunEvent.CREATE, idempotency_key="run:create")

    def lease(self, owner: str = "harness", expires_at: float | None = None) -> None:
        self.emit(
            RunEvent.LEASE,
            {"lease_owner": owner, "lease_expires_at": expires_at},
            "run:lease",
        )

    def skip_unsupported(self, reason: str = "unsupported") -> None:
        self.emit(RunEvent.SKIP_UNSUPPORTED, {"reason": reason}, "run:skip")

    def begin_reset(self) -> None:
        self.emit(RunEvent.BEGIN_RESET, idempotency_key="run:reset")

    def reset_ok(self) -> None:
        self.emit(RunEvent.RESET_OK, idempotency_key="run:reset-ok")

    def reset_failed(self, reason: str) -> None:
        self.emit(RunEvent.RESET_FAILED, {"reason": reason}, "run:reset-failed")

    def baseline_ok(self) -> None:
        self.emit(RunEvent.BASELINE_OK, idempotency_key="run:baseline-ok")

    def baseline_failed(self, reason: str) -> None:
        self.emit(RunEvent.BASELINE_FAILED, {"reason": reason}, "run:baseline-failed")

    def inject_ok(self) -> None:
        self.emit(RunEvent.INJECT_OK, idempotency_key="run:inject-ok")

    def inject_failed(self, reason: str) -> None:
        self.emit(RunEvent.INJECT_FAILED, {"reason": reason}, "run:inject-failed")

    def fault_observed(self) -> None:
        self.emit(RunEvent.FAULT_OBSERVED, idempotency_key="run:fault-observed")

    def fault_not_observed(self, reason: str = RunEndReason.FAULT_NOT_OBSERVED.value) -> None:
        self.emit(RunEvent.FAULT_NOT_OBSERVED, {"reason": reason}, "run:fault-not-observed")

    def agent_done(self, gate_terminal: str = "", gate_reason: str | None = None) -> None:
        self.emit(
            RunEvent.AGENT_DONE,
            {"gate_terminal": gate_terminal, "gate_reason": gate_reason},
            "run:agent-done",
        )

    def agent_terminal(self, reason: str) -> None:
        self.emit(RunEvent.AGENT_TERMINAL, {"reason": reason}, f"run:agent-terminal:{reason}")

    def provider_error_all(self, attempts: Any = ()) -> None:
        self.emit(RunEvent.PROVIDER_ERROR_ALL, {"attempts": list(attempts)}, "run:provider-error-all")

    def verified(self) -> None:
        self.emit(RunEvent.VERIFY_OK, idempotency_key="run:verify-ok")

    def verification_failed(self) -> None:
        self.emit(RunEvent.VERIFY_FAILED, idempotency_key="run:verify-failed")

    def abort(self, reason: str) -> None:
        self.emit(RunEvent.ABORT, {"reason": reason}, f"run:abort:{reason}")

    def abandon(self, actor: str = "operator") -> None:
        self.emit(
            RunEvent.ABANDON,
            {"actor": actor, "reason": "abandoned"},
            idempotency_key="run:abandon",
        )

    def illegal_transition(self, machine: str, event: str, error: str) -> None:
        self.emit(
            RunEvent.ILLEGAL_TRANSITION,
            {"machine": machine, "event": event, "error": error},
            f"run:illegal:{machine}:{event}",
        )

    def lease_expired(
        self,
        previous_state: RunPhase | None = None,
        lease_expires_at: float | None = None,
        *,
        reason: str = "lease_expired",
        detected_by: str = "reaper",
    ) -> None:
        payload: dict[str, Any] = {
            "lease_expires_at": lease_expires_at,
            "reason": reason,
            "detected_by": detected_by,
        }
        if previous_state is not None:
            payload["previous_state"] = previous_state.value
        key = f"reap:{self.run_id}:{previous_state.value if previous_state else ''}:{lease_expires_at}"
        self.emit(RunEvent.LEASE_EXPIRED, payload, key)

    def reap(
        self,
        previous_state: RunPhase | None = None,
        lease_expires_at: float | None = None,
    ) -> None:
        payload: dict[str, Any] = {"lease_expires_at": lease_expires_at}
        if previous_state is not None:
            payload["previous_state"] = previous_state.value
        key = f"reap:{self.run_id}:{previous_state.value if previous_state else ''}:{lease_expires_at}"
        self.emit(RunEvent.REAP, payload, key)

    def resume_action(self, unknown_effect: bool = False) -> RunCrashAction:
        previous = self.state.previous_state or self.state.phase
        return resume_triage(previous, unknown_effect)

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "state": _state_to_dict(self.state),
            "events": [_event_to_dict(event) for event in self.events],
        }

    @classmethod
    def from_snapshot(
        cls,
        run_id: str,
        snapshot: Mapping[str, Any] | None,
        log: Callable[[dict[str, Any]], int] | None = None,
    ) -> "RunRuntime":
        runtime = cls(run_id=run_id, log=log)
        if not isinstance(snapshot, Mapping):
            return runtime
        events = snapshot.get("events") or []
        if events:
            for item in events:
                runtime._replay(item)
        else:
            runtime.state = _state_from_dict(snapshot.get("state"))
        return runtime

    @classmethod
    def from_run(
        cls,
        run_id: str,
        row: Mapping[str, Any],
        log: Callable[[dict[str, Any]], int] | None = None,
    ) -> "RunRuntime":
        runtime = cls(run_id=run_id, log=log)
        runtime.create()
        runtime.lease(row.get("lease_owner") or "harness", row.get("lease_expires_at"))
        status = str(row.get("status") or "pending")
        wire_phase = str(row.get("phase") or "queued")
        reason = row.get("end_reason")
        if status == "skipped_unsupported":
            runtime.skip_unsupported(str(reason or "unsupported"))
            return runtime
        target = next(
            (phase for phase, projection in RUN_PROJECTION.items() if projection[1] == wire_phase),
            next(
                (phase for phase, projection in RUN_PROJECTION.items() if projection[0] == status),
                RunPhase.LEASED,
            ),
        )
        target_rank = RUN_STATES[target].rank
        for phase in RUN_PROJECTION:
            if phase is RunPhase.PENDING or phase is RunPhase.LEASED:
                continue
            if RUN_STATES[phase].rank <= target_rank:
                if phase is RunPhase.RESETTING:
                    runtime.begin_reset()
                elif phase is RunPhase.BASELINING:
                    runtime.reset_ok()
                elif phase is RunPhase.INJECTING:
                    runtime.baseline_ok()
                elif phase is RunPhase.FAULT_CHECK:
                    runtime.inject_ok()
                elif phase is RunPhase.AGENT_RUNNING:
                    runtime.fault_observed()
                elif phase is RunPhase.VERIFYING:
                    runtime.agent_done(str(reason or ""), None)
                if phase is target:
                    break
        if status == "finished":
            if reason == "verify_ok" or row.get("verify_ok") in (True, 1):
                runtime.verified()
            else:
                runtime.verification_failed()
        elif status == "aborted":
            runtime.abort(str(reason or "aborted"))
        elif status == "interrupted":
            runtime.lease_expired(target, row.get("lease_expires_at"))
        return runtime

    @classmethod
    def from_events(
        cls,
        run_id: str,
        events: list[dict[str, Any]],
        log: Callable[[dict[str, Any]], int] | None = None,
    ) -> "RunRuntime":
        runtime = cls(run_id=run_id, log=log)
        for row in events:
            if not str(row.get("type") or "").startswith("run."):
                continue
            payload = row.get("payload")
            if not isinstance(payload, Mapping):
                continue
            runtime._replay({
                "type": payload.get("event"),
                "payload": payload.get("payload"),
                "idempotency_key": payload.get("idempotency_key"),
                "seq": payload.get("seq"),
            })
        return runtime

    def _replay(self, item: Any) -> None:
        if not isinstance(item, Mapping):
            return
        try:
            event_type = RunEvent(str(item.get("type")))
        except ValueError:
            return
        payload = item.get("payload")
        if not isinstance(payload, Mapping):
            payload = {}
        event = MachineEvent(
            event_type,
            dict(payload),
            idempotency_key=item.get("idempotency_key"),
            seq=item.get("seq"),
        )
        try:
            transition = RUN_MACHINE.transition(self.state, event)
        except IllegalTransition as exc:
            self.rejections.append(str(exc))
            return
        self.state = transition.state
        self.events.append(event)
        if event.idempotency_key is not None:
            self._seen_keys.add(event.idempotency_key)
