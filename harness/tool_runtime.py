from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping

from harness.machines.tools import (
    TOOL_MACHINE,
    TOOL_PROJECTION,
    TOOL_STATES,
    ToolCrashAction,
    ToolEvent,
    ToolPhase,
    ToolState,
)
from harness.state_machine import IllegalTransition, MachineEvent, rejection_event


@dataclass(frozen=True)
class ToolFacts:
    call_id: str
    phase: ToolPhase
    status: str
    result_code: str | None
    terminal: bool
    crash_action: ToolCrashAction
    mutating: bool
    destructive: bool
    reason: str | None
    violation_kind: str | None


def canonical_tool_event(event: MachineEvent[ToolEvent], call_id: str) -> dict[str, Any]:
    payload = dict(event.payload)
    payload.setdefault("call_id", call_id)
    envelope: dict[str, Any] = {
        "event": event.type.value,
        "payload": payload,
        "call_id": call_id,
    }
    if event.idempotency_key is not None:
        envelope["idempotency_key"] = event.idempotency_key
    if event.seq is not None:
        envelope["seq"] = event.seq
    return {"type": f"tool.{event.type.value}", "payload": envelope}


def _event_to_dict(event: MachineEvent[ToolEvent], call_id: str) -> dict[str, Any]:
    data: dict[str, Any] = {
        "type": event.type.value,
        "payload": dict(event.payload),
        "call_id": call_id,
    }
    if event.idempotency_key is not None:
        data["idempotency_key"] = event.idempotency_key
    if event.seq is not None:
        data["seq"] = event.seq
    return data


def _state_to_dict(state: ToolState) -> dict[str, Any]:
    data = asdict(state)
    data["phase"] = state.phase.value
    data["visited"] = [phase.value for phase in state.visited]
    return data


def _state_from_dict(data: Mapping[str, Any] | None) -> ToolState:
    if not isinstance(data, Mapping):
        return ToolState()
    values = dict(data)
    phase = values.get("phase")
    if isinstance(phase, str):
        try:
            values["phase"] = ToolPhase(phase)
        except ValueError:
            values.pop("phase", None)
    visited = values.get("visited")
    if isinstance(visited, list):
        parsed: list[ToolPhase] = []
        for item in visited:
            try:
                parsed.append(ToolPhase(str(item)))
            except ValueError:
                continue
        values["visited"] = tuple(parsed) or (ToolPhase.INTENDED,)
    allowed = {name for name in ToolState.__dataclass_fields__}
    return ToolState(**{key: value for key, value in values.items() if key in allowed})


@dataclass
class ToolRuntime:
    call_id: str
    name: str = ""
    mutating: bool = False
    destructive: bool = False
    state: ToolState = field(default_factory=ToolState)
    events: list[MachineEvent[ToolEvent]] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)
    _seen_keys: set[str] = field(default_factory=set, repr=False)
    _seen_seqs: set[int] = field(default_factory=set, repr=False)
    log: Callable[[dict[str, Any]], int] | None = field(default=None, repr=False)

    @property
    def phase(self) -> ToolPhase:
        return self.state.phase

    @property
    def status(self) -> str:
        return self.state.persisted_status

    @property
    def result_code(self) -> str | None:
        return self.state.result_code

    @property
    def terminal(self) -> bool:
        return TOOL_STATES[self.state.phase].terminal

    @property
    def failed(self) -> bool:
        return self.state.phase in {
            ToolPhase.FAILED,
            ToolPhase.DENIED,
            ToolPhase.UNKNOWN_EFFECT,
        }

    @property
    def facts(self) -> ToolFacts:
        spec = TOOL_STATES[self.state.phase]
        return ToolFacts(
            call_id=self.call_id,
            phase=self.state.phase,
            status=self.state.persisted_status,
            result_code=self.state.result_code,
            terminal=spec.terminal,
            crash_action=spec.crash_action,
            mutating=self.state.mutating,
            destructive=self.state.destructive,
            reason=self.state.reason,
            violation_kind=self.state.violation_kind,
        )

    def projection(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "name": self.name,
            "phase": self.state.phase.value,
            "status": self.state.persisted_status,
            "result_code": self.state.result_code,
            "mutating": self.state.mutating,
            "destructive": self.state.destructive,
            "reason": self.state.reason,
            "violation_kind": self.state.violation_kind,
            "terminal": self.terminal,
        }

    def emit(
        self,
        event_type: ToolEvent,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        seq: int | None = None,
    ) -> MachineEvent[ToolEvent] | None:
        if idempotency_key is not None and idempotency_key in self._seen_keys:
            return None
        if seq is not None and seq in self._seen_seqs:
            return None
        event = MachineEvent(event_type, dict(payload or {}), idempotency_key=idempotency_key, seq=seq)
        try:
            transition = TOOL_MACHINE.transition(self.state, event)
        except IllegalTransition as exc:
            self.rejections.append(str(exc))
            if self.log is not None:
                self.log(rejection_event(TOOL_MACHINE, self.state, event, str(exc)))
            return None
        if idempotency_key is not None:
            self._seen_keys.add(idempotency_key)
        if seq is not None:
            self._seen_seqs.add(seq)
        self.events.append(event)
        self.state = transition.state
        if self.log is not None:
            self.log(canonical_tool_event(event, self.call_id))
        return event

    def prepare(self, name: str = "", mutating: bool = False, destructive: bool = False) -> None:
        if name:
            self.name = name
        self.mutating = bool(mutating)
        self.destructive = bool(destructive)
        if self.events:
            return
        self.emit(
            ToolEvent.PREPARE,
            {"name": self.name, "mutating": self.mutating, "destructive": self.destructive},
            f"tool:prepare:{self.call_id}",
        )

    def start(self) -> None:
        if self.state.phase is ToolPhase.INTENDED:
            self.emit(ToolEvent.START, idempotency_key=f"tool:start:{self.call_id}")

    def success(self) -> None:
        self.emit(ToolEvent.SUCCESS, idempotency_key=f"tool:success:{self.call_id}")

    def failure(self, code: str = "failed", reason: str | None = None) -> None:
        payload: dict[str, Any] = {"code": code}
        if reason is not None:
            payload["reason"] = reason
        self.emit(ToolEvent.FAILURE, payload, f"tool:failure:{self.call_id}:{code}")

    def deny(self, reason: str, violation_kind: str | None = None) -> None:
        self.emit(
            ToolEvent.DENY,
            {"reason": reason, "violation_kind": violation_kind},
            f"tool:deny:{self.call_id}",
        )

    def cancel(self) -> None:
        self.emit(ToolEvent.CANCEL, {"reason": "cancelled"}, f"tool:cancel:{self.call_id}")

    def lease_expired(self) -> None:
        self.emit(
            ToolEvent.LEASE_EXPIRED,
            {"reason": "lease expired before outcome"},
            f"tool:lease-expired:{self.call_id}",
        )

    def resume_supersede(self, reason: str = "superseded_after_resume") -> None:
        self.emit(
            ToolEvent.RESUME_SUPERSEDE,
            {"reason": reason},
            f"tool:resume-supersede:{self.call_id}",
        )

    def apply_outcome(self, outcome: Any) -> None:
        denied = bool(getattr(outcome, "denied", False)) or bool(getattr(outcome, "policy_violation", None))
        code = str(getattr(outcome, "result_code", None) or "")
        error = getattr(outcome, "call_error", None)
        if denied:
            self.deny(str(error or getattr(outcome, "message", "denied")), getattr(outcome, "violation_kind", None))
        elif error or code in {"failed", "unknown_tool"}:
            self.failure(code or "failed", str(error) if error else None)
        else:
            self.success()

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mutating": self.mutating,
            "destructive": self.destructive,
            "state": _state_to_dict(self.state),
            "events": [_event_to_dict(event, self.call_id) for event in self.events],
        }

    @classmethod
    def from_snapshot(
        cls,
        call_id: str,
        snapshot: Mapping[str, Any] | None,
        log: Callable[[dict[str, Any]], int] | None = None,
    ) -> "ToolRuntime":
        runtime = cls(call_id=call_id, log=log)
        if not isinstance(snapshot, Mapping):
            return runtime
        runtime.name = str(snapshot.get("name") or "")
        runtime.mutating = bool(snapshot.get("mutating"))
        runtime.destructive = bool(snapshot.get("destructive"))
        events = snapshot.get("events") or []
        if events:
            for item in events:
                runtime._replay(item)
        else:
            runtime.state = _state_from_dict(snapshot.get("state"))
        return runtime

    def _replay(self, item: Any) -> None:
        if not isinstance(item, Mapping):
            return
        idempotency_key = item.get("idempotency_key")
        seq = item.get("seq")
        if idempotency_key is not None and idempotency_key in self._seen_keys:
            return
        if seq is not None and int(seq) in self._seen_seqs:
            return
        try:
            event_type = ToolEvent(str(item.get("type")))
        except ValueError:
            return
        payload = item.get("payload")
        if not isinstance(payload, Mapping):
            payload = {}
        if event_type is ToolEvent.PREPARE:
            self.name = str(payload.get("name") or self.name)
            self.mutating = bool(payload.get("mutating", self.mutating))
            self.destructive = bool(payload.get("destructive", self.destructive))
        event = MachineEvent(
            event_type,
            dict(payload),
            idempotency_key=idempotency_key,
            seq=seq,
        )
        try:
            transition = TOOL_MACHINE.transition(self.state, event)
        except IllegalTransition as exc:
            self.rejections.append(str(exc))
            return
        self.state = transition.state
        self.events.append(event)
        if event.idempotency_key is not None:
            self._seen_keys.add(event.idempotency_key)
        if event.seq is not None:
            self._seen_seqs.add(int(event.seq))


@dataclass
class ToolLedger:
    run_id: str
    log: Callable[[dict[str, Any]], int] | None = None
    calls: dict[str, ToolRuntime] = field(default_factory=dict)
    rejections: list[str] = field(default_factory=list)

    def for_call(
        self,
        call_id: str,
        name: str = "",
        mutating: bool = False,
        destructive: bool = False,
    ) -> ToolRuntime:
        runtime = self.calls.get(call_id)
        if runtime is None:
            runtime = ToolRuntime(call_id=call_id, log=self.log)
            self.calls[call_id] = runtime
        runtime.prepare(name, mutating, destructive)
        return runtime

    def get(self, call_id: str) -> ToolRuntime | None:
        return self.calls.get(call_id)

    def merge_rows(self, rows: list[Mapping[str, Any]]) -> None:
        for row in rows:
            call_id = str(row.get("call_id") or "")
            if not call_id:
                continue
            runtime = self.calls.get(call_id)
            if runtime is None:
                runtime = ToolRuntime(call_id=call_id, log=self.log)
                self.calls[call_id] = runtime
            runtime.name = str(row.get("name") or runtime.name)
            runtime.mutating = bool(row.get("mutating", runtime.mutating))
            runtime.destructive = bool(row.get("destructive", runtime.destructive))
            if not runtime.events:
                runtime.state = _state_from_row(row, runtime.mutating, runtime.destructive)

    def merge_snapshot(self, snapshot: Mapping[str, Any] | None) -> None:
        if not isinstance(snapshot, Mapping):
            return
        for call_id, item in snapshot.items():
            if not isinstance(item, Mapping):
                continue
            runtime = ToolRuntime.from_snapshot(str(call_id), item, log=self.log)
            self.calls[str(call_id)] = runtime
            self.rejections.extend(runtime.rejections)

    def projections(self) -> dict[str, dict[str, Any]]:
        return {call_id: runtime.projection() for call_id, runtime in self.calls.items()}

    def unfinished(self) -> dict[str, dict[str, Any]]:
        return {
            call_id: projection
            for call_id, projection in self.projections().items()
            if not projection["terminal"]
        }

    def has_unknown_mutating(self) -> bool:
        for runtime in self.calls.values():
            projection = runtime.projection()
            if not projection["mutating"]:
                continue
            if projection["result_code"] == "unknown_effect":
                return True
            if not projection["terminal"]:
                return True
        return False

    def to_snapshot(self) -> dict[str, Any]:
        return {call_id: runtime.to_snapshot() for call_id, runtime in self.calls.items()}

    @classmethod
    def from_events(
        cls,
        run_id: str,
        events: list[Mapping[str, Any]],
        log: Callable[[dict[str, Any]], int] | None = None,
    ) -> "ToolLedger":
        ledger = cls(run_id=run_id, log=log)
        for row in events:
            if str(row.get("type") or "").split(".", 1)[0] != "tool":
                continue
            payload = row.get("payload")
            if not isinstance(payload, Mapping):
                continue
            call_id = str(payload.get("call_id") or "")
            if not call_id:
                continue
            runtime = ledger.calls.get(call_id)
            if runtime is None:
                runtime = ToolRuntime(call_id=call_id, log=log)
                ledger.calls[call_id] = runtime
            runtime.name = str(payload.get("name") or runtime.name)
            runtime.mutating = bool(payload.get("mutating", runtime.mutating))
            runtime.destructive = bool(payload.get("destructive", runtime.destructive))
            inner = payload.get("payload")
            runtime._replay({
                "type": payload.get("event"),
                "payload": inner if isinstance(inner, Mapping) else {},
                "idempotency_key": payload.get("idempotency_key"),
                "seq": payload.get("seq"),
            })
        ledger.rejections.extend(
            rejection
            for runtime in ledger.calls.values()
            for rejection in runtime.rejections
        )
        return ledger


_TOOL_STATUS_PHASE = {
    status: phase for phase, (status, _code) in TOOL_PROJECTION.items()
}
_TOOL_STATUS_PHASE.update({
    "unknown": ToolPhase.UNKNOWN_EFFECT,
    "superseded_after_resume": ToolPhase.SUPERSEDED,
})


def _state_from_row(row: Mapping[str, Any], mutating: bool, destructive: bool) -> ToolState:
    status = str(row.get("status") or "proposed")
    result_code = row.get("result_code")
    if result_code == "unknown_effect":
        phase = ToolPhase.UNKNOWN_EFFECT
    elif result_code == "superseded_after_resume":
        phase = ToolPhase.SUPERSEDED
    else:
        phase_value = row.get("phase") or _TOOL_STATUS_PHASE.get(status, ToolPhase.INTENDED)
        try:
            phase = ToolPhase(str(phase_value))
        except ValueError:
            phase = _TOOL_STATUS_PHASE.get(status, ToolPhase.INTENDED)
    projected_status, projected_code = TOOL_PROJECTION[phase]
    return ToolState(
        phase=phase,
        persisted_status=status or projected_status,
        result_code=result_code or projected_code,
        mutating=bool(mutating),
        destructive=bool(destructive),
        reason=row.get("reason"),
        violation_kind=row.get("violation_kind"),
        visited=(ToolPhase.INTENDED,) if phase is ToolPhase.INTENDED else (ToolPhase.INTENDED, phase),
    )
