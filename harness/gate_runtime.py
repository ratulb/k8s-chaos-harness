from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping

from harness.machines.gate import (
    GATE_MACHINE,
    MAX_NUDGES,
    GateEvent,
    GatePhase,
    GateState,
    GateViolation,
    receipt_for,
)
from harness.state_machine import IllegalTransition, MachineEvent


@dataclass(frozen=True)
class RepairFacts:
    canonical_name: str
    acceptable: bool
    forbidden: bool


@dataclass(frozen=True)
class RepairResultFacts:
    status: str
    denied: bool = False
    timed_out: bool = False
    unknown_effect: bool = False
    detail: str | None = None


def _scenario_value(scenario: Any, name: str, default: Any) -> Any:
    if isinstance(scenario, Mapping):
        return scenario.get(name, default)
    return getattr(scenario, name, default)


def _name_set(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        value = value.keys()
    return {str(item) for item in value or ()}


def classify_repair(
    canonical_name: Any,
    scenario: Any,
    denial_reason: str | None = None,
) -> RepairFacts:
    name = str(canonical_name or "repair")
    acceptable = _name_set(_scenario_value(scenario, "acceptable_repairs", ()))
    forbidden = _name_set(_scenario_value(scenario, "forbidden_repairs", ()))
    text = str(denial_reason or "").lower()
    is_forbidden = name in forbidden or "forbidden" in text
    is_acceptable = name in acceptable if acceptable else not is_forbidden
    if "not acceptable" in text or "unacceptable" in text:
        is_acceptable = False
    return RepairFacts(name, is_acceptable and not is_forbidden, is_forbidden)


def classify_repair_result(
    status: Any = None,
    result: Any = None,
    error: Any = None,
) -> RepairResultFacts:
    wire_status = str(status or "").strip().lower()
    text = str(result or "")
    detail = str(error) if error else None
    if wire_status == "denied" or text.startswith("BLOCKED:") or "approval not granted" in text.lower():
        return RepairResultFacts("denied", denied=True, detail=detail or text or None)
    if wire_status in {"timeout", "timed_out"} or text.startswith("timeout waiting for"):
        return RepairResultFacts("timeout", timed_out=True, detail=detail or text or None)
    if wire_status in {"unknown", "proposed", "started"}:
        return RepairResultFacts("unknown_effect", unknown_effect=True, detail=detail or text or None)
    if wire_status == "failed" or text.startswith("ERROR:") or detail:
        return RepairResultFacts("failed", detail=detail or text or None)
    return RepairResultFacts("success", detail=text or None)


def repair_facts_from_tool_state(state: Any) -> RepairResultFacts:
    phase = str(getattr(getattr(state, "phase", None), "value", getattr(state, "phase", "")) or "")
    code = str(getattr(state, "result_code", None) or "")
    reason = getattr(state, "reason", None)
    detail = str(reason) if reason else None
    if phase == "denied" or code == "denied":
        return RepairResultFacts("denied", denied=True, detail=detail)
    if code == "timeout":
        return RepairResultFacts("timeout", timed_out=True, detail=detail)
    if code == "unknown_effect" or phase == "unknown_effect":
        return RepairResultFacts("unknown_effect", unknown_effect=True, detail=detail)
    if phase == "failed" or code == "failed":
        return RepairResultFacts("failed", detail=detail)
    return RepairResultFacts("success", detail=detail)


def _gate_state_to_dict(state: GateState) -> dict[str, Any]:
    data = asdict(state)
    data["phase"] = state.phase.value
    data["visited"] = [phase.value for phase in state.visited]
    data["violations"] = [violation.value for violation in state.violations]
    return data


def _event_to_dict(event: MachineEvent[GateEvent]) -> dict[str, Any]:
    data: dict[str, Any] = {"type": event.type.value, "payload": dict(event.payload)}
    if event.idempotency_key is not None:
        data["idempotency_key"] = event.idempotency_key
    return data


def canonical_gate_event(event: MachineEvent[GateEvent]) -> dict[str, Any]:
    payload: dict[str, Any] = {"event": event.type.value, "payload": dict(event.payload)}
    if event.idempotency_key is not None:
        payload["idempotency_key"] = event.idempotency_key
    return {"type": f"gate.{event.type.value}", "payload": payload}


@dataclass
class GateRuntime:
    run_id: str
    state: GateState = field(default_factory=GateState)
    events: list[MachineEvent[GateEvent]] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)
    _seen_keys: set[str] = field(default_factory=set, repr=False)
    log: Callable[[dict[str, Any]], int] | None = field(default=None, repr=False)

    @property
    def receipt(self) -> str | None:
        return receipt_for(self.run_id) if self.state.receipt_issued else None

    @property
    def approval_source(self) -> str | None:
        for event in reversed(self.events):
            if event.type is GateEvent.DIAGNOSIS_SUBMITTED:
                return event.payload.get("source")
        return None

    @property
    def diagnosis_raw(self) -> Any | None:
        for event in reversed(self.events):
            if event.type is GateEvent.DIAGNOSIS_SUBMITTED:
                return event.payload.get("raw")
        return None

    @property
    def accepted_repair(self) -> str | None:
        for event in reversed(self.events):
            if event.type is not GateEvent.REPAIR_PROPOSED:
                continue
            if event.payload.get("forbidden") or event.payload.get("denial_reason"):
                continue
            if not event.payload.get("acceptable"):
                continue
            name = event.payload.get("canonical_name")
            return str(name) if name else None
        return None

    @property
    def repair_args(self) -> dict[str, Any] | None:
        for event in reversed(self.events):
            if event.type is not GateEvent.REPAIR_PROPOSED:
                continue
            if event.payload.get("forbidden") or event.payload.get("denial_reason"):
                continue
            if not event.payload.get("acceptable"):
                continue
            args = event.payload.get("args")
            if isinstance(args, Mapping):
                return dict(args)
        return None

    @property
    def nudged(self) -> bool:
        return any(
            event.type in {GateEvent.NUDGE, GateEvent.BUDGET_EXHAUSTED}
            and event.payload.get("detail") != "diagnosis_cap"
            for event in self.events
        )

    @property
    def policy_violations(self) -> list[str]:
        details: list[str] = []
        for event in self.events:
            if event.type is not GateEvent.REPAIR_PROPOSED:
                continue
            payload = event.payload
            name = str(payload.get("canonical_name") or "repair")
            reason = str(payload.get("denial_reason") or "")
            if payload.get("forbidden") or "forbidden" in reason.lower():
                details.append(f"{name}: forbidden repair for this scenario")
            elif not payload.get("acceptable"):
                details.append(f"{name}: repair not acceptable for this scenario")
        if details:
            return details
        messages = {
            GateViolation.PREMATURE_REPAIR: "diagnosis required first",
            GateViolation.FORBIDDEN_REPAIR: "forbidden repair for this scenario",
            GateViolation.UNACCEPTABLE_REPAIR: "repair not acceptable for this scenario",
            GateViolation.REPAIR_AFTER_NO_FAULT: "no fault detected; repair forbidden",
        }
        return [f"{self.state.repair_name or 'repair'}: {messages[item]}" for item in self.state.violations]

    @property
    def forbidden_reason(self) -> str | None:
        for detail in self.policy_violations:
            if "forbidden" in detail.lower():
                return detail
        return None

    def emission(self) -> tuple[MachineEvent[GateEvent], ...]:
        return tuple(self.events)

    def emit(
        self,
        event_type: GateEvent,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> MachineEvent[GateEvent] | None:
        if idempotency_key is not None and idempotency_key in self._seen_keys:
            return None
        event = MachineEvent(event_type, dict(payload or {}), idempotency_key=idempotency_key)
        try:
            transition = GATE_MACHINE.transition(self.state, event)
        except IllegalTransition as exc:
            self.rejections.append(str(exc))
            return None
        if idempotency_key is not None:
            self._seen_keys.add(idempotency_key)
        self.events.append(event)
        self.state = transition.state
        if self.log is not None:
            self.log(canonical_gate_event(event))
        return event

    def submit_diagnosis(
        self,
        *,
        label: str | None,
        correct: bool,
        approved: bool,
        attempts: int,
        first_attempt_correct: bool = False,
        is_no_fault: bool = False,
        source: str | None = None,
        raw: Any = None,
    ) -> MachineEvent[GateEvent] | None:
        return self.emit(
            GateEvent.DIAGNOSIS_SUBMITTED,
            {
                "label": label,
                "correct": bool(correct),
                "approved": bool(approved),
                "is_no_fault": bool(is_no_fault),
                "attempts": int(attempts),
                "first_attempt_correct": bool(first_attempt_correct),
                "source": source,
                "raw": raw,
            },
            f"diagnosis:{attempts}",
        )

    def nudge(self, *, step: int | None = None, kind: str = "budget") -> MachineEvent[GateEvent] | None:
        if self.state.phase in {GatePhase.REPAIRED, GatePhase.CLEARED, GatePhase.ESCALATED}:
            return None
        if self.state.nudge_count >= MAX_NUDGES:
            return self.emit(
                GateEvent.BUDGET_EXHAUSTED,
                {"reason": "budget_tokens", "detail": "nudge_cap", "step": step},
                "nudge:cap",
            )
        key = f"nudge:{kind}:{step}" if step is not None else f"nudge:{kind}:{self.state.nudge_count + 1}"
        return self.emit(GateEvent.NUDGE, {"step": step, "kind": kind}, key)

    def propose_repair(
        self,
        name: str,
        args: Mapping[str, Any] | None,
        scenario: Any,
        denial_reason: str | None = None,
    ) -> MachineEvent[GateEvent] | None:
        facts = classify_repair(name, scenario, denial_reason)
        return self.emit(
            GateEvent.REPAIR_PROPOSED,
            {
                "canonical_name": facts.canonical_name,
                "acceptable": facts.acceptable,
                "forbidden": facts.forbidden,
                "args": dict(args or {}),
                "denial_reason": denial_reason,
            },
            f"repair-proposed:{facts.canonical_name}:{denial_reason or 'allowed'}",
        )

    def finish_repair(
        self,
        *,
        status: Any = None,
        result: Any = None,
        error: Any = None,
        tool_state: Any = None,
        idempotency_key: str | None = None,
    ) -> MachineEvent[GateEvent] | None:
        facts = (
            repair_facts_from_tool_state(tool_state)
            if tool_state is not None
            else classify_repair_result(status, result, error)
        )
        if facts.denied:
            return None
        if facts.unknown_effect:
            return self.emit(
                GateEvent.UNKNOWN_EFFECT,
                {"reason": facts.detail or "unknown effect", "result_code": facts.status},
                idempotency_key,
            )
        return self.emit(
            GateEvent.REPAIR_FINISHED,
            {
                "status": facts.status,
                "timed_out": facts.timed_out,
                "result_summary": facts.detail,
            },
            idempotency_key,
        )

    def escalate(self, reason: str) -> MachineEvent[GateEvent] | None:
        return self.emit(GateEvent.ESCALATE, {"reason": reason}, f"escalate:{reason}")

    def to_snapshot(self) -> dict[str, Any]:
        return {"events": [_event_to_dict(event) for event in self.events]}

    @classmethod
    def from_snapshot(
        cls,
        run_id: str,
        snapshot: Mapping[str, Any] | None,
        log: Callable[[dict[str, Any]], int] | None = None,
    ) -> "GateRuntime":
        runtime = cls(run_id=run_id, log=log)
        for item in snapshot.get("events") or () if snapshot else ():
            if not isinstance(item, Mapping):
                continue
            try:
                event_type = GateEvent(str(item.get("type")))
            except ValueError:
                continue
            runtime.emit(event_type, item.get("payload"), item.get("idempotency_key"))
        return runtime

    @classmethod
    def from_events(
        cls,
        run_id: str,
        events: list[dict[str, Any]],
        log: Callable[[dict[str, Any]], int] | None = None,
    ) -> "GateRuntime":
        runtime = cls(run_id=run_id, log=log)
        for row in events:
            if not str(row.get("type") or "").startswith("gate."):
                continue
            payload = row.get("payload")
            if not isinstance(payload, Mapping):
                continue
            try:
                event_type = GateEvent(str(payload.get("event")))
            except ValueError:
                continue
            runtime.emit(
                event_type,
                payload.get("payload"),
                payload.get("idempotency_key"),
            )
        return runtime
