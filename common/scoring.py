from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from tools.naming import normalize_repair_name

SCORER_VERSION = "strict-loop-v2"

ACCEPTABLE_BY_TRUTH: dict[str, set[str]] = {
    "no_fault": set(),
    "etcd_member_unreachable": {"restart_member"},
    "etcd_config_error": {"restart_member", "member_remove_readd"},
    "etcd_member_data_loss": {"member_remove_readd"},
    "etcd_disk_exhaustion": {"restart_member", "member_remove_readd"},
    "etcd_quorum_loss": {"restart_member", "full_cluster_reset"},
}

EXCLUDED_END_REASONS = {
    "error",
    "fault_not_observed",
    "injection_failed",
}

BUDGET_END_REASONS = {
    "budget_steps",
    "budget_tokens",
}


def is_excluded(record: dict[str, Any], require_fault: bool = True) -> bool:
    reason = str(record.get("end_reason") or "")
    if reason.startswith("provider_error") or reason in EXCLUDED_END_REASONS:
        return True
    if require_fault and record.get("fault_present") is not True:
        return True
    return bool(record.get("status") == "skipped_unsupported" or "unsupported" in reason)


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
    policy_violations: int
    loop_success: bool
    strict_loop_success: bool

    @classmethod
    def from_record(
        cls,
        record: dict[str, Any],
        acceptable: set[str] | None = None,
    ) -> "Score":
        truth = str(record.get("truth") or "")
        raw_repair = record.get("repair")
        repair = normalize_repair_name(raw_repair) if raw_repair else None
        allowed = acceptable if acceptable is not None else ACCEPTABLE_BY_TRUTH.get(truth, set())
        repair_acceptable = bool(record["repair_acceptable"]) if "repair_acceptable" in record else (
            repair is None if truth == "no_fault" else bool(repair and repair in allowed)
        )
        diagnosis_correct = bool(record.get("diagnosis_correct"))
        first_attempt = record.get("diagnosis_first_attempt_correct")
        attempts = record.get("diagnosis_attempts")
        if first_attempt is not None:
            first_attempt_correct = bool(first_attempt)
        elif attempts is not None:
            first_attempt_correct = int(attempts) <= 1 and diagnosis_correct
        else:
            first_attempt_correct = diagnosis_correct
        recovered = bool(record.get("verify_ok"))
        no_fault = truth == "no_fault"
        loop_success = bool(diagnosis_correct and recovered and (bool(repair) != no_fault))
        forbidden = bool(record.get("repair_forbidden") or record.get("forbidden_reason"))
        policy_violations = len(record.get("policy_violations") or [])
        reason = str(record.get("end_reason") or "")
        strict_loop_success = bool(
            loop_success
            and repair_acceptable
            and record.get("approval_granted")
            and first_attempt_correct
            and not is_excluded(record)
            and reason not in BUDGET_END_REASONS
            and not forbidden
            and not policy_violations
        )
        return cls(
            scorer_version=str(record.get("scorer_version") or SCORER_VERSION),
            fault_present=bool(record.get("fault_present")),
            end_reason=reason,
            diagnosis=record.get("diagnosis"),
            diagnosis_correct=diagnosis_correct,
            diagnosis_attempts=int(record.get("diagnosis_attempts") or 0),
            diagnosis_first_attempt_correct=first_attempt_correct,
            repair_action=repair,
            repair_acceptable=repair_acceptable,
            repair_forbidden=forbidden,
            recovered=recovered,
            policy_violations=policy_violations,
            loop_success=loop_success,
            strict_loop_success=strict_loop_success,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
