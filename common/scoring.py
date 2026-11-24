from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from harness.gate_runtime import GatePath
from tools.naming import normalize_repair_name

SCORER_VERSION = "gate-path-v1"

STRICT_TERMINALS = {"repaired", "no_fault_cleared"}
STRICT_END_REASON = "verify_ok"

EXCLUDED_END_REASONS = {
    "diagnosis_attempts_exhausted",
    "budget_tokens",
    "unknown_effect",
    "abandoned",
    "state_machine_bug",
    "provider_error_all",
    "fault_not_observed",
    "reset_failed",
    "baseline_failed",
    "injection_failed",
    "lease_expired",
    "interrupted_unknown_previous_state",
    "unsupported",
    "verify_failed",
}

BUDGET_END_REASONS = {
    "budget_steps",
    "budget_tokens",
}


def _run_state(record: Mapping[str, Any]) -> dict[str, Any]:
    value = record.get("run")
    if not isinstance(value, Mapping):
        return {}
    state = value.get("state")
    return dict(state) if isinstance(state, Mapping) else {}


def _tool_states(record: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    value = record.get("tool_states")
    if not isinstance(value, Mapping):
        return {}
    return {
        str(call_id): dict(state)
        for call_id, state in value.items()
        if isinstance(state, Mapping)
    }


def _gate_path(record: Mapping[str, Any]) -> GatePath:
    return GatePath.from_dict(record.get("gate_path")) or GatePath(
        visited=(),
        terminal="",
        terminal_reason=None,
        diagnosis_submitted=False,
        first_attempt_correct=False,
        receipt_issued=False,
        repair_proposed=False,
        repair_executed=False,
        repair_status=None,
        violations=(),
    )


def _end_reason(record: Mapping[str, Any]) -> str:
    return str(_run_state(record).get("end_reason") or "")


def is_excluded(record: dict[str, Any], require_fault: bool = True) -> bool:
    state = _run_state(record)
    reason = _end_reason(record)
    status = str(state.get("status") or "")
    if reason.startswith("provider_error") or reason in EXCLUDED_END_REASONS or reason in BUDGET_END_REASONS:
        return True
    if require_fault and not bool(state.get("fault_present")):
        return True
    return status in {"aborted", "skipped_unsupported"} or "unsupported" in reason


@dataclass(frozen=True)
class Score:
    scorer_version: str
    fault_present: bool
    end_reason: str
    diagnosis: str | None
    diagnosis_correct: bool
    diagnosis_attempts: int
    diagnosis_first_attempt_correct: bool
    repair_action: str | None
    repair_acceptable: bool
    repair_forbidden: bool
    recovered: bool
    gate_terminal: str
    receipt_issued: bool
    repair_executed: bool
    policy_violations: int
    loop_success: bool
    strict_loop_success: bool

    @classmethod
    def from_record(
        cls,
        record: dict[str, Any],
        acceptable: set[str] | None = None,
    ) -> "Score":
        run_state = _run_state(record)
        gate_path = _gate_path(record)
        tool_states = _tool_states(record)
        truth = str(record.get("truth") or "")
        no_fault = truth == "no_fault"
        raw_repair = record.get("repair")
        repair = normalize_repair_name(raw_repair) if raw_repair else None
        violations = list(gate_path.violations)
        for state in tool_states.values():
            kind = state.get("violation_kind")
            if kind:
                violations.append(f"{state.get('name') or 'tool'}: {kind}")
        violation_text = " ".join(str(item).lower() for item in violations)
        forbidden = "forbidden" in violation_text or bool(gate_path.terminal_reason == "forbidden_repair")
        unacceptable = "not acceptable" in violation_text or "unacceptable" in violation_text
        if acceptable is not None and repair and not no_fault:
            policy_repair_ok = repair in acceptable
        else:
            policy_repair_ok = bool(repair)
        repair_acceptable = bool(
            not forbidden
            and not unacceptable
            and (not gate_path.repair_proposed if no_fault else policy_repair_ok)
        )
        diagnosis_correct = bool(record.get("diagnosis_correct"))
        fault_present = bool(run_state.get("fault_present"))
        verify_ok = bool(run_state.get("verify_ok"))
        reason = _end_reason(record)
        loop_success = bool(diagnosis_correct and verify_ok and (bool(repair) != no_fault))
        strict = bool(
            fault_present
            and run_state.get("status") == "finished"
            and run_state.get("phase") == "finished"
            and gate_path.terminal in STRICT_TERMINALS
            and (gate_path.terminal != "repaired" or "approved" in gate_path.visited)
            and gate_path.first_attempt_correct
            and gate_path.receipt_issued
            and verify_ok
            and not violations
            and reason == STRICT_END_REASON
            and reason not in BUDGET_END_REASONS
            and not is_excluded(record)
            and not forbidden
            and repair_acceptable
            and (gate_path.repair_executed if not no_fault else not gate_path.repair_proposed)
        )
        return cls(
            scorer_version=SCORER_VERSION,
            fault_present=fault_present,
            end_reason=reason,
            diagnosis=record.get("diagnosis"),
            diagnosis_correct=diagnosis_correct,
            diagnosis_attempts=int(record.get("diagnosis_attempts") or 0),
            diagnosis_first_attempt_correct=gate_path.first_attempt_correct,
            repair_action=repair,
            repair_acceptable=repair_acceptable,
            repair_forbidden=forbidden,
            recovered=verify_ok,
            gate_terminal=gate_path.terminal,
            receipt_issued=gate_path.receipt_issued,
            repair_executed=gate_path.repair_executed,
            policy_violations=len(violations),
            loop_success=loop_success,
            strict_loop_success=strict,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
