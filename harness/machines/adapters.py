from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from harness.machines.gate import GATE_MACHINE, GateEvent, GatePhase, GateState
from harness.machines.run import RUN_MACHINE, RunEvent, RunState
from harness.machines.tools import TOOL_MACHINE, ToolEvent, ToolState
from harness.state_machine import IllegalTransition, Machine, MachineEvent, MachineKind

NUDGE_MARKERS = (
    "You have not submitted a diagnosis",
    "Stop investigating now",
)
BUDGET_REASONS = {"budget_steps", "budget_tokens"}
PROVIDER_PREFIX = "provider_error"
UNSUPPORTED_MARKERS = ("unsupported", "not supported")
TERMINAL_RUN_STATUSES = {"finished", "aborted", "skipped_unsupported", "interrupted"}


@dataclass(frozen=True)
class DiagnosisFacts:
    label: str | None
    correct: bool
    is_no_fault: bool
    attempts: int
    first_attempt_correct: bool
    approved: bool


@dataclass(frozen=True)
class RepairFacts:
    canonical_name: str
    acceptable: bool
    forbidden: bool


@dataclass(frozen=True)
class RepairResultFacts:
    status: str
    timed_out: bool = False
    denied: bool = False
    unknown_effect: bool = False
    detail: str | None = None


@dataclass(frozen=True)
class FallbackProvenance:
    chain_backend: str | None = None
    chain_model: str | None = None
    requested_backend: str | None = None
    responder_backend: str | None = None
    fallback_from: tuple[str, ...] = ()
    attempted_backends: tuple[str, ...] = ()
    skipped_backends: tuple[str, ...] = ()

    @property
    def uses_fallback(self) -> bool:
        return bool(self.fallback_from) or (
            self.responder_backend is not None
            and self.responder_backend != self.requested_backend
        )

    @property
    def preserves_chain_identity(self) -> bool:
        return self.chain_backend == self.requested_backend


@dataclass(frozen=True)
class ShadowMismatch:
    machine: str
    field: str
    expected: Any
    actual: Any
    detail: str = ""


@dataclass(frozen=True)
class MachineRejection:
    machine: str
    event: str
    reason: str


@dataclass
class ShadowReport:
    run_id: str
    gate: GateState
    run: RunState
    tools: dict[str, ToolState]
    gate_events: tuple[MachineEvent[GateEvent], ...]
    run_events: tuple[MachineEvent[RunEvent], ...]
    tool_events: dict[str, tuple[MachineEvent[ToolEvent], ...]]
    provenance: FallbackProvenance
    mismatches: tuple[ShadowMismatch, ...] = ()
    rejections: tuple[MachineRejection, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.mismatches


def _as_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return dict(row)


def _event_type(row: Mapping[str, Any]) -> str:
    return str(row.get("type") or "")


def _event_seq(row: Mapping[str, Any]) -> int | None:
    value = row.get("seq")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _scenario_value(
    run: Mapping[str, Any],
    scenario: Mapping[str, Any] | Any | None,
    key: str,
    default: Any,
) -> Any:
    if isinstance(scenario, Mapping) and key in scenario:
        return scenario[key]
    if scenario is not None and hasattr(scenario, key):
        return getattr(scenario, key)
    return run.get(key, default)


def _name_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, Mapping):
        value = value.keys()
    if isinstance(value, str):
        return {value}
    try:
        return {str(item) for item in value}
    except TypeError:
        return {str(value)}


def _canonical_name(name: Any) -> str:
    from tools.naming import normalize_repair_name

    return normalize_repair_name(str(name or ""))


def classify_diagnosis(
    event: Mapping[str, Any],
    run: Mapping[str, Any] | None = None,
    scenario: Mapping[str, Any] | Any | None = None,
) -> DiagnosisFacts:
    run = run or {}
    granted = _bool(event.get("granted"))
    correct_value = event.get("diagnosis_correct")
    if correct_value is None:
        correct_value = event.get("correct")
    correct = granted if correct_value is None else _bool(correct_value)
    label = event.get("diagnosis")
    if label is None:
        label = event.get("label")
    truth = _scenario_value(run, scenario, "root_cause", run.get("truth"))
    is_no_fault = _bool(
        event.get(
            "is_no_fault",
            label == "no_fault" or truth == "no_fault",
        )
    )
    attempts = _int(
        event.get("diagnosis_attempts", run.get("diagnosis_attempts")),
        default=1,
    ) or 1
    first_value = event.get("diagnosis_first_attempt_correct")
    first = correct and attempts == 1 if first_value is None else _bool(first_value)
    approved = granted if "approved" not in event else _bool(event.get("approved"))
    return DiagnosisFacts(
        label=str(label) if label is not None else None,
        correct=correct,
        is_no_fault=is_no_fault,
        attempts=attempts,
        first_attempt_correct=first,
        approved=approved,
    )


def classify_repair(
    canonical_name: Any,
    args: Mapping[str, Any] | None = None,
    scenario: Mapping[str, Any] | Any | None = None,
    denial_reason: str | None = None,
) -> RepairFacts:
    run: dict[str, Any] = {}
    name = _canonical_name(canonical_name)
    acceptable = _name_set(_scenario_value(run, scenario, "acceptable_repairs", ()))
    forbidden = _name_set(_scenario_value(run, scenario, "forbidden_repairs", ()))
    text = str(denial_reason or "")
    is_forbidden = name in forbidden or "forbidden" in text.lower()
    is_acceptable = name in acceptable if acceptable else not is_forbidden
    if "not acceptable" in text.lower() or "unacceptable" in text.lower():
        is_acceptable = False
    return RepairFacts(
        canonical_name=name,
        acceptable=is_acceptable and not is_forbidden,
        forbidden=is_forbidden,
    )


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


def _fold_tolerant(
    machine: Machine[Any, Any, Any, Any],
    events: Iterable[MachineEvent[Any]],
    initial: Any = None,
) -> tuple[Any, tuple[MachineRejection, ...]]:
    state = machine.initial if initial is None else initial
    seen: set[str] = set()
    last_seq: int | None = None
    rejections: list[MachineRejection] = []

    for event in events:
        dedupe_key: str | None = None
        if event.seq is not None:
            if last_seq is not None and event.seq <= last_seq:
                continue
            last_seq = event.seq
            dedupe_key = f"seq:{event.seq}"
        elif event.idempotency_key is not None:
            dedupe_key = f"key:{event.idempotency_key}"
        if dedupe_key is not None:
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
        try:
            state = machine.transition(state, event).state
        except IllegalTransition as exc:
            rejections.append(
                MachineRejection(machine.kind.value, str(getattr(event.type, "value", event.type)), str(exc))
            )
    return state, tuple(rejections)


def _machine_events(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted((_as_dict(row) for row in rows), key=lambda row: (_event_seq(row) is None, _event_seq(row) or 0))


def _is_nudge(row: Mapping[str, Any]) -> bool:
    event_type = _event_type(row)
    role = str(row.get("role") or "")
    if event_type == "nudge":
        return True
    if event_type not in {"user", "message"} and role not in {"user", "message"}:
        return False
    content = str(row.get("content") or (row.get("message") or {}).get("content") or "")
    return any(marker in content for marker in NUDGE_MARKERS)


def _is_mutating_tool(row: Mapping[str, Any]) -> bool:
    mutating = row.get("mutating")
    if mutating is not None:
        return _bool(mutating)
    name = _canonical_name(row.get("name"))
    return "repair" in name or "restart" in name or "restore" in name


def _repair_event(row: Mapping[str, Any], scenario: Mapping[str, Any] | Any | None) -> bool:
    if _event_type(row) == "tool":
        return _is_mutating_tool(row)
    return _is_mutating_tool(row)


def _terminal_reason_events(
    run: Mapping[str, Any],
    gate: GateState,
    events: Sequence[Mapping[str, Any]],
) -> list[MachineEvent[GateEvent]]:
    if gate.phase in {GatePhase.REPAIRED, GatePhase.CLEARED, GatePhase.ESCALATED}:
        return []
    reason = str(run.get("end_reason") or "")
    if reason in BUDGET_REASONS or reason.startswith("budget_"):
        return [
            MachineEvent(
                GateEvent.BUDGET_EXHAUSTED,
                {"reason": "budget_tokens", "detail": reason},
                idempotency_key="terminal:budget",
            )
        ]
    if reason.startswith(PROVIDER_PREFIX):
        return [
            MachineEvent(
                GateEvent.ESCALATE,
                {"reason": "provider_error_all"},
                idempotency_key="terminal:provider",
            )
        ]
    if reason in {"operator_abort", "state_discard", "abandoned", "lease_expired"}:
        return [
            MachineEvent(
                GateEvent.ABORT,
                {"reason": "abandoned" if reason != "lease_expired" else reason, "actor": "operator"},
                idempotency_key="terminal:abort",
            )
        ]
    for row in events:
        if _event_type(row) in {"operator_abort", "resume_aborted", "state_discard_abort"}:
            return [
                MachineEvent(
                    GateEvent.ABORT,
                    {"reason": row.get("reason") or "abandoned", "actor": "operator"},
                    idempotency_key=f"terminal:{_event_type(row)}",
                )
            ]
    return []


def legacy_gate_events(
    events: Iterable[Mapping[str, Any]],
    run: Mapping[str, Any],
    tool_calls: Iterable[Mapping[str, Any]] = (),
    messages: Iterable[Mapping[str, Any]] = (),
    scenario: Mapping[str, Any] | Any | None = None,
) -> list[MachineEvent[GateEvent]]:
    rows = _machine_events(events)
    ordered: list[tuple[int, int, MachineEvent[GateEvent]]] = []
    counter = 0

    def add(anchor: int, event: MachineEvent[GateEvent]) -> None:
        nonlocal counter
        ordered.append((anchor, counter, event))
        counter += 1

    for row in rows:
        if _event_type(row) != "approval":
            continue
        facts = classify_diagnosis(row, run, scenario)
        seq = _event_seq(row)
        add(
            seq or 0,
            MachineEvent(
                GateEvent.DIAGNOSIS_SUBMITTED,
                {
                    "label": facts.label,
                    "correct": facts.correct,
                    "approved": facts.approved,
                    "is_no_fault": facts.is_no_fault,
                    "attempts": facts.attempts,
                    "first_attempt_correct": facts.first_attempt_correct,
                    "source": row.get("mode") or "legacy",
                },
                seq=seq,
                idempotency_key=f"approval:{seq}" if seq is not None else None,
            ),
        )

    explicit_nudges = [row for row in rows if _event_type(row) == "nudge"]
    if explicit_nudges:
        for row in explicit_nudges:
            seq = _event_seq(row)
            add(
                seq or 0,
                MachineEvent(
                    GateEvent.NUDGE,
                    {"step": row.get("step"), "kind": row.get("kind") or "budget"},
                    seq=seq,
                    idempotency_key=f"nudge:{seq}" if seq is not None else None,
                ),
            )
    else:
        nudge_count = 0
        for row in sorted(messages, key=lambda item: _int(item.get("seq"), 0)):
            if not _is_nudge(row):
                continue
            if nudge_count >= 2:
                break
            nudge_count += 1
            message_seq = row.get("seq")
            add(
                _int(message_seq, 0),
                MachineEvent(
                    GateEvent.NUDGE,
                    {"step": None, "kind": "no_tool", "reconstructed": True},
                    idempotency_key=f"message-nudge:{message_seq}",
                ),
            )

    repair_rows = [row for row in rows if _repair_event(row, scenario)]
    for row in repair_rows:
        seq = _event_seq(row)
        key = f"tool:{seq}" if seq is not None else f"tool:{row.get('name')}:{id(row)}"
        result_status = row.get("result") or row.get("result_sent")
        outcome = classify_repair_result(row.get("status"), result_status, row.get("error"))
        facts = classify_repair(row.get("name"), row.get("args"), scenario, outcome.detail)
        add(
            seq or 0,
            MachineEvent(
                GateEvent.REPAIR_PROPOSED,
                {
                    "canonical_name": facts.canonical_name,
                    "acceptable": facts.acceptable,
                    "forbidden": facts.forbidden,
                    "args": row.get("args"),
                },
                seq=seq,
                idempotency_key=f"{key}:proposed",
            ),
        )
        if outcome.unknown_effect:
            add(
                seq or 0,
                MachineEvent(
                    GateEvent.UNKNOWN_EFFECT,
                    {"tool_call_id": row.get("call_id") or key, "reason": outcome.detail},
                    idempotency_key=f"{key}:unknown",
                ),
            )
        elif outcome.status != "denied":
            add(
                seq or 0,
                MachineEvent(
                    GateEvent.REPAIR_FINISHED,
                    {
                        "status": outcome.status,
                        "timed_out": outcome.timed_out,
                        "result_summary": outcome.detail,
                    },
                    idempotency_key=f"{key}:finished",
                ),
            )

    if not any(_event_type(row) == "tool" for row in rows):
        for call in _machine_events(tool_calls):
            if not _is_mutating_tool(call):
                continue
            call_id = str(call.get("call_id") or call.get("name") or id(call))
            outcome = classify_repair_result(
                call.get("status"),
                call.get("result_sent") or call.get("result_raw"),
                call.get("error"),
            )
            facts = classify_repair(call.get("name"), call.get("args"), scenario, outcome.detail)
            add(
                0,
                MachineEvent(
                    GateEvent.REPAIR_PROPOSED,
                    {
                        "canonical_name": facts.canonical_name,
                        "acceptable": facts.acceptable,
                        "forbidden": facts.forbidden,
                        "args": call.get("args"),
                    },
                    idempotency_key=f"tool-call:{call_id}:proposed",
                ),
            )
            if outcome.unknown_effect:
                add(
                    0,
                    MachineEvent(
                        GateEvent.UNKNOWN_EFFECT,
                        {"tool_call_id": call_id, "reason": outcome.detail},
                        idempotency_key=f"tool-call:{call_id}:unknown",
                    ),
                )
            elif outcome.status != "denied":
                add(
                    0,
                    MachineEvent(
                        GateEvent.REPAIR_FINISHED,
                        {"status": outcome.status, "timed_out": outcome.timed_out, "result_summary": outcome.detail},
                        idempotency_key=f"tool-call:{call_id}:finished",
                    ),
                )

    result = [event for _anchor, _order, event in sorted(ordered)]
    gate_after_events, _ = _fold_tolerant(GATE_MACHINE, result)
    result.extend(_terminal_reason_events(run, gate_after_events, rows))
    return result


def legacy_tool_events(
    tool_calls: Iterable[Mapping[str, Any]],
) -> dict[str, list[MachineEvent[ToolEvent]]]:
    grouped: dict[str, list[MachineEvent[ToolEvent]]] = {}
    for call in _machine_events(tool_calls):
        call_id = str(call.get("call_id") or call.get("name") or id(call))
        mutating = _is_mutating_tool(call)
        prefix = f"tool-call:{call_id}"
        events = grouped.setdefault(call_id, [])
        events.append(
            MachineEvent(
                ToolEvent.PREPARE,
                {
                    "name": call.get("name"),
                    "mutating": mutating,
                    "destructive": _bool(call.get("destructive")),
                },
                idempotency_key=f"{prefix}:prepare",
            )
        )
        status = str(call.get("status") or "proposed").strip().lower()
        outcome = classify_repair_result(
            status,
            call.get("result_sent") or call.get("result_raw"),
            call.get("error"),
        )
        if status == "denied" or outcome.denied:
            events.append(
                MachineEvent(
                    ToolEvent.DENY,
                    {"reason": outcome.detail or "denied", "violation_kind": status},
                    idempotency_key=f"{prefix}:deny",
                )
            )
            continue

        pending = status in {"proposed", "started", "unknown"} or outcome.unknown_effect
        if pending:
            if status != "proposed" or call.get("started_at") is not None:
                events.append(
                    MachineEvent(ToolEvent.START, {}, idempotency_key=f"{prefix}:start")
                )
            if mutating:
                events.append(
                    MachineEvent(
                        ToolEvent.CANCEL,
                        {"reason": status, "tool_call_id": call_id},
                        idempotency_key=f"{prefix}:cancel",
                    )
                )
            else:
                events.append(
                    MachineEvent(
                        ToolEvent.RESUME_SUPERSEDE,
                        {"reason": "superseded_after_resume"},
                        idempotency_key=f"{prefix}:supersede",
                    )
                )
            continue

        events.append(MachineEvent(ToolEvent.START, {}, idempotency_key=f"{prefix}:start"))
        if outcome.status in {"failed", "timeout"}:
            events.append(
                MachineEvent(
                    ToolEvent.FAILURE,
                    {"code": outcome.status, "detail": outcome.detail},
                    idempotency_key=f"{prefix}:failure",
                )
            )
        else:
            events.append(
                MachineEvent(
                    ToolEvent.SUCCESS,
                    {"status": outcome.status},
                    idempotency_key=f"{prefix}:success",
                )
            )
    return grouped


def _setup_value(row: Mapping[str, Any]) -> bool:
    for key in ("ok", "present", "success"):
        if key in row:
            return _bool(row.get(key))
    return False


SETUP_EVENTS = {
    "reset": (RunEvent.RESET_OK, RunEvent.RESET_FAILED),
    "baseline": (RunEvent.BASELINE_OK, RunEvent.BASELINE_FAILED),
    "inject": (RunEvent.INJECT_OK, RunEvent.INJECT_FAILED),
}


def legacy_run_events(
    events: Iterable[Mapping[str, Any]],
    run: Mapping[str, Any],
    gate: GateState,
    provenance: FallbackProvenance | None = None,
) -> list[MachineEvent[RunEvent]]:
    rows = _machine_events(events)
    result = [MachineEvent(RunEvent.CREATE)]
    reason = str(run.get("end_reason") or "")
    status = str(run.get("status") or "")
    setup_types = {"reset", "baseline", "inject", "fault_check", "setup", "assistant", "tool"}
    started = (
        bool(run.get("lease_owner"))
        or status not in {"", "pending", "queued"}
        or any(_event_type(row) in setup_types for row in rows)
    )
    if started:
        result.append(
            MachineEvent(
                RunEvent.LEASE,
                {
                    "lease_owner": run.get("lease_owner") or "legacy",
                    "lease_expires_at": run.get("lease_expires_at"),
                },
            )
        )
    if status == "skipped_unsupported" or "unsupported" in reason.lower():
        result.append(MachineEvent(RunEvent.SKIP_UNSUPPORTED, {"reason": reason or "unsupported"}))
        return result

    by_type: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        by_type.setdefault(_event_type(row), []).append(row)

    if by_type.get("reset") or run.get("reset_ok") is not None:
        result.append(MachineEvent(RunEvent.BEGIN_RESET, {}, idempotency_key="setup:reset:begin"))

    setup_failed = False
    scalar_setup = (
        ("reset", "reset_ok"),
        ("baseline", "baseline_ok"),
        ("inject", "inject_ok"),
    )
    for kind, scalar_key in scalar_setup:
        kind_rows = by_type.get(kind, [])
        success, failure = SETUP_EVENTS[kind]
        if kind_rows:
            first = kind_rows[0]
            seq = _event_seq(first)
            ok = _setup_value(first)
            result.append(
                MachineEvent(
                    success if ok else failure,
                    {"kind": kind},
                    seq=seq,
                    idempotency_key=f"setup:{kind}:{seq}",
                )
            )
        elif run.get(scalar_key) is not None:
            ok = _bool(run.get(scalar_key))
            result.append(
                MachineEvent(
                    success if ok else failure,
                    {"kind": kind, "source": "scalar"},
                    idempotency_key=f"setup:{kind}:scalar",
                )
            )
        if any(not _setup_value(row) for row in kind_rows) or (
            not kind_rows and run.get(scalar_key) is not None and not _bool(run.get(scalar_key))
        ):
            setup_failed = True
            break
    if setup_failed:
        return result

    abort_types = ("operator_abort", "resume_aborted", "state_discard_abort")
    abort_rows = [row for kind in abort_types for row in by_type.get(kind, [])]
    if abort_rows:
        abort = abort_rows[0]
        abort_seq = _event_seq(abort)
        result.append(
            MachineEvent(
                RunEvent.ABORT,
                {"reason": abort.get("reason") or reason or "abandoned", "actor": "operator"},
                seq=abort_seq,
                idempotency_key=f"abort:{_event_type(abort)}:{abort_seq}",
            )
        )
        return result

    fault_rows = by_type.get("fault_check", [])
    if fault_rows:
        fault = fault_rows[0]
        observed = _setup_value(fault)
        seq = _event_seq(fault)
        result.append(
            MachineEvent(
                RunEvent.FAULT_OBSERVED if observed else RunEvent.FAULT_NOT_OBSERVED,
                {},
                seq=seq,
                idempotency_key=f"fault-check:{seq}",
            )
        )
    elif run.get("fault_present") is not None:
        result.append(
            MachineEvent(
                RunEvent.FAULT_OBSERVED if _bool(run.get("fault_present")) else RunEvent.FAULT_NOT_OBSERVED,
                {},
                idempotency_key="fault-check:scalar",
            )
        )

    if gate.phase in {GatePhase.REPAIRED, GatePhase.CLEARED}:
        result.append(
            MachineEvent(
                RunEvent.AGENT_DONE,
                {"gate_terminal": gate.phase.value, "gate_reason": gate.end_reason},
                idempotency_key="agent:done",
            )
        )
    elif gate.phase is GatePhase.ESCALATED:
        result.append(
            MachineEvent(
                RunEvent.AGENT_TERMINAL,
                {"reason": gate.end_reason or reason or "gate_escalation"},
                idempotency_key="agent:terminal",
            )
        )
    elif reason.startswith(PROVIDER_PREFIX):
        attempts: list[dict[str, Any]] = []
        for row in rows:
            if _event_type(row) != "llm_error":
                continue
            for attempt in row.get("attempts") or ():
                if isinstance(attempt, Mapping):
                    attempts.append(_as_dict(attempt))
        result.append(
            MachineEvent(
                RunEvent.PROVIDER_ERROR_ALL,
                {"attempts": attempts, "reason": reason},
                idempotency_key="agent:provider-error",
            )
        )
    elif status in TERMINAL_RUN_STATUSES or by_type.get("record") or by_type.get("verify_started"):
        result.append(
            MachineEvent(
                RunEvent.AGENT_TERMINAL,
                {"reason": reason or "agent_stopped"},
                idempotency_key="agent:terminal",
            )
        )

    if gate.phase in {GatePhase.REPAIRED, GatePhase.CLEARED}:
        verify_rows = by_type.get("verify", [])
        verify_value = run.get("verify_ok")
        if verify_rows:
            verify = verify_rows[0]
            ok = _bool(verify.get("ok"))
            seq = _event_seq(verify)
        elif verify_value is not None:
            ok = _bool(verify_value)
            seq = None
        else:
            ok = None
            seq = None
        if ok is not None:
            result.append(
                MachineEvent(
                    RunEvent.VERIFY_OK if ok else RunEvent.VERIFY_FAILED,
                    {},
                    seq=seq,
                    idempotency_key=f"verify:{seq}" if seq is not None else "verify:scalar",
                )
            )
    return result


def _extract_provenance(events: Iterable[Mapping[str, Any]], run: Mapping[str, Any]) -> FallbackProvenance:
    rows = _machine_events(events)
    assistant = next((row for row in rows if _event_type(row) == "assistant"), None)
    llm_error = next((row for row in rows if _event_type(row) == "llm_error"), None)
    source = assistant or llm_error or {}
    attempted: list[str] = []
    for row in rows:
        if _event_type(row) != "llm_error":
            continue
        for attempt in row.get("attempts") or ():
            backend = attempt.get("backend") if isinstance(attempt, Mapping) else getattr(attempt, "backend", None)
            if backend:
                attempted.append(str(backend))
    fallback_from = source.get("fallback_from") or []
    if isinstance(fallback_from, str):
        fallback_from = [fallback_from]
    skipped = source.get("skipped_backends") or []
    if isinstance(skipped, str):
        skipped = [skipped]
    return FallbackProvenance(
        chain_backend=run.get("backend"),
        chain_model=run.get("model"),
        requested_backend=source.get("requested_backend") or run.get("backend"),
        responder_backend=source.get("backend"),
        fallback_from=tuple(str(item) for item in fallback_from),
        attempted_backends=tuple(attempted),
        skipped_backends=tuple(str(item) for item in skipped),
    )


def _add_mismatch(
    mismatches: list[ShadowMismatch],
    machine: str,
    field_name: str,
    expected: Any,
    actual: Any,
    detail: str = "",
) -> None:
    if expected != actual:
        mismatches.append(ShadowMismatch(machine, field_name, expected, actual, detail))


def _compare_run(
    run: Mapping[str, Any],
    state: RunState,
    mismatches: list[ShadowMismatch],
    *,
    has_verify: bool,
) -> None:
    for field_name, expected in (
        ("status", state.status),
        ("phase", state.wire_phase),
        ("end_reason", state.end_reason),
    ):
        actual = run.get(field_name)
        if actual is not None:
            _add_mismatch(mismatches, "run", field_name, expected, actual)
    if run.get("verify_ok") is not None and (has_verify or _bool(run.get("verify_ok"))):
        _add_mismatch(mismatches, "run", "verify_ok", state.verify_ok, _bool(run.get("verify_ok")))
    if run.get("fault_present") is not None:
        _add_mismatch(mismatches, "run", "fault_present", state.fault_present, _bool(run.get("fault_present")))


def _compare_gate(
    run: Mapping[str, Any],
    state: GateState,
    mismatches: list[ShadowMismatch],
    *,
    has_diagnosis: bool,
) -> None:
    submitted = state.diagnosis_attempts > 0
    for field_name, expected in (
        ("diagnosis_submitted", submitted),
        ("diagnosis_correct", state.diagnosis_correct),
        ("diagnosis_attempts", state.diagnosis_attempts),
        ("diagnosis_first_attempt_correct", state.first_attempt_correct),
        ("approval_granted", state.receipt_issued),
    ):
        actual = run.get(field_name)
        if actual is None:
            continue
        if field_name == "diagnosis_correct" and not has_diagnosis and not _bool(actual):
            continue
        if isinstance(expected, bool):
            actual = _bool(actual)
        elif isinstance(expected, int):
            actual = _int(actual)
        _add_mismatch(mismatches, "gate", field_name, expected, actual)
    if run.get("repair") is not None:
        _add_mismatch(mismatches, "gate", "repair", state.repair_name, run.get("repair"))
    if run.get("forbidden_reason") and state.violations:
        if "forbidden" not in " ".join(item.value for item in state.violations):
            _add_mismatch(mismatches, "gate", "forbidden_reason", "forbidden_violation", run.get("forbidden_reason"))


def _compare_tools(
    tool_calls: Iterable[Mapping[str, Any]],
    states: Mapping[str, ToolState],
    mismatches: list[ShadowMismatch],
) -> None:
    for call in tool_calls:
        call_id = str(call.get("call_id") or call.get("name") or id(call))
        state = states.get(call_id)
        if state is None:
            continue
        actual = str(call.get("status") or "")
        if actual == "unknown":
            continue
        _add_mismatch(mismatches, "tool", f"{call_id}:status", state.persisted_status, actual)


def shadow_replay(
    run: Mapping[str, Any],
    events: Iterable[Mapping[str, Any]] = (),
    tool_calls: Iterable[Mapping[str, Any]] = (),
    messages: Iterable[Mapping[str, Any]] = (),
    scenario: Mapping[str, Any] | Any | None = None,
) -> ShadowReport:
    run_id = str(run.get("run_id") or "")
    event_rows = list(events)
    call_rows = list(tool_calls)
    message_rows = list(messages)
    provenance = _extract_provenance(event_rows, run)

    gate_events = legacy_gate_events(event_rows, run, call_rows, message_rows, scenario)
    gate, gate_rejections = _fold_tolerant(GATE_MACHINE, gate_events)
    has_diagnosis = any(
        event.type is GateEvent.DIAGNOSIS_SUBMITTED for event in gate_events
    )
    has_verify = any(_event_type(row) in {"verify", "verify_started"} for row in event_rows)

    tool_event_map = legacy_tool_events(call_rows)
    tool_states: dict[str, ToolState] = {}
    tool_rejections: list[MachineRejection] = []
    for call_id, call_events in tool_event_map.items():
        state, rejected = _fold_tolerant(TOOL_MACHINE, call_events)
        tool_states[call_id] = state
        tool_rejections.extend(rejected)

    run_events = legacy_run_events(event_rows, run, gate, provenance)
    run_state, run_rejections = _fold_tolerant(RUN_MACHINE, run_events)

    mismatches: list[ShadowMismatch] = []
    _compare_gate(run, gate, mismatches, has_diagnosis=has_diagnosis)
    _compare_run(run, run_state, mismatches, has_verify=has_verify)
    _compare_tools(call_rows, tool_states, mismatches)

    return ShadowReport(
        run_id=run_id,
        gate=gate,
        run=run_state,
        tools=tool_states,
        gate_events=tuple(gate_events),
        run_events=tuple(run_events),
        tool_events={key: tuple(value) for key, value in tool_event_map.items()},
        provenance=provenance,
        mismatches=tuple(mismatches),
        rejections=gate_rejections + tuple(tool_rejections) + run_rejections,
    )
