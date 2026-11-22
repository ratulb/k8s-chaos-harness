from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from common.vocab import ROOT_CAUSE_LABELS
from harness.prompt import SystemPrompt
from tools.naming import normalize_repair_name
from tools.tool import Tool, ToolOutcome

MAX_DIAGNOSIS_ATTEMPTS = 2


def normalize_diagnosis(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    lowered = text.lower()
    if lowered in ROOT_CAUSE_LABELS:
        return lowered
    for label in ROOT_CAUSE_LABELS:
        if label in lowered or label.replace("_", " ") in lowered:
            return label
    if "no fault" in lowered or "cluster healthy" in lowered or "healthy" in lowered:
        return "no_fault"
    return text


@dataclass
class AgentState:
    scenario: dict[str, Any]
    run_id: str
    approve: str
    diagnosis: str | None = None
    diagnosis_raw: Any = None
    diagnosis_submitted: bool = False
    diagnosis_correct: bool = False
    diagnosis_attempts: int = 0
    diagnosis_first_attempt_correct: bool = False
    approval_token: str | None = None
    approval_source: str | None = None
    repair: str | None = None
    repair_args: dict[str, Any] | None = None
    policy_violations: list[str] = field(default_factory=list)
    forbidden_reason: str | None = None

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any], scenario: dict[str, Any], run_id: str, approve: str) -> "AgentState":
        diagnosis = snapshot.get("diagnosis")
        return cls(
            scenario=scenario,
            run_id=run_id,
            approve=approve,
            diagnosis=diagnosis,
            diagnosis_raw=snapshot.get("diagnosis_raw"),
            diagnosis_submitted=bool(snapshot.get("diagnosis_submitted", diagnosis is not None)),
            diagnosis_correct=bool(snapshot.get("diagnosis_correct", False)),
            diagnosis_attempts=int(snapshot.get("diagnosis_attempts", 0)),
            diagnosis_first_attempt_correct=bool(snapshot.get("diagnosis_first_attempt_correct", False)),
            approval_token=snapshot.get("approval_token"),
            approval_source=snapshot.get("approval_source"),
            repair=snapshot.get("repair"),
            repair_args=snapshot.get("repair_args"),
            policy_violations=list(snapshot.get("policy_violations") or []),
            forbidden_reason=snapshot.get("forbidden_reason"),
        )

    def snapshot(
        self,
        trace_path: str,
        seq: int,
        steps: int,
        tokens_used: int,
        diagnosis_nudged: bool,
    ) -> dict[str, Any]:
        return {
            "diagnosis": self.diagnosis,
            "diagnosis_raw": self.diagnosis_raw,
            "diagnosis_submitted": self.diagnosis_submitted,
            "diagnosis_attempts": self.diagnosis_attempts,
            "diagnosis_first_attempt_correct": self.diagnosis_first_attempt_correct,
            "approval_token": self.approval_token,
            "diagnosis_correct": self.diagnosis_correct,
            "approval_source": self.approval_source,
            "repair": self.repair,
            "repair_args": self.repair_args,
            "policy_violations": self.policy_violations,
            "forbidden_reason": self.forbidden_reason,
            "steps": steps,
            "tokens_used": tokens_used,
            "diagnosis_nudged": diagnosis_nudged,
            "trace_path": trace_path,
            "seq": seq,
        }

    def apply(self, outcome: ToolOutcome) -> None:
        if outcome.diagnosis_submitted:
            self.diagnosis = outcome.diagnosis
            self.diagnosis_raw = outcome.diagnosis_raw
            self.diagnosis_submitted = True
            self.diagnosis_correct = bool(outcome.diagnosis_correct)
            self.diagnosis_attempts = outcome.diagnosis_attempts or self.diagnosis_attempts
            self.diagnosis_first_attempt_correct = (
                outcome.diagnosis_first_attempt_correct or self.diagnosis_first_attempt_correct
            )
            self.approval_token = outcome.approval_token
            self.approval_source = outcome.approval_source
        if outcome.repair is not None:
            self.repair = outcome.repair
            self.repair_args = outcome.repair_args
        if outcome.policy_violation:
            self.policy_violations.append(outcome.policy_violation)
        if outcome.forbidden_reason:
            self.forbidden_reason = outcome.forbidden_reason


class Diagnosis:
    def __init__(
        self,
        state: AgentState,
        store: Any = None,
        log: Callable[[dict[str, Any]], int] | None = None,
        pause_reporter: Any = None,
        resume_reporter: Any = None,
    ) -> None:
        self.state = state
        self.store = store
        self.log = log
        self.pause_reporter = pause_reporter
        self.resume_reporter = resume_reporter

    def execute(self, tool: Tool, args: dict[str, Any], call_id: str) -> ToolOutcome:
        raw = args.get("root_cause")
        diagnosis = normalize_diagnosis(raw)
        attempt = self.state.diagnosis_attempts + 1
        exhausted = self.state.diagnosis_attempts >= MAX_DIAGNOSIS_ATTEMPTS
        correct = not exhausted and diagnosis == self.state.scenario["root_cause"]
        first_attempt_correct = correct and attempt == 1
        approved = correct
        approval_source = None
        if approved and self.state.approve == "prompt":
            if self.pause_reporter is not None:
                self.pause_reporter()
            try:
                try:
                    answer = input(f"Approve repair for {diagnosis}? [y/N] ").strip().lower()
                except EOFError:
                    answer = ""
            finally:
                if self.resume_reporter is not None:
                    self.resume_reporter()
            approved = answer in {"y", "yes"}
            approval_source = "operator"
        elif approved:
            approval_source = "policy"
        approval_token = f"approved:{self.state.run_id}" if approved else None
        if exhausted:
            message = "diagnosis attempts exhausted; report findings and escalate without repair"
        elif not correct:
            message = "diagnosis rejected; gather new evidence before resubmitting; do not brute-force labels"
        elif self.state.approve == "prompt" and not approved:
            message = "diagnosis correct; operator declined repair approval"
        else:
            message = f"diagnosis accepted; approval_token={approval_token}"
        if self.log is not None:
            self.log({
                "type": "approval",
                "granted": approved,
                "diagnosis": diagnosis,
                "diagnosis_correct": correct,
                "diagnosis_attempts": attempt,
                "diagnosis_first_attempt_correct": first_attempt_correct,
                "approval_token": approval_token,
                "mode": self.state.approve,
                "raw": raw,
            })
        if self.store is not None:
            self.store.add_diagnosis_attempt(self.state.run_id, raw, diagnosis, correct)
            self.store.update_tool_call_status(self.state.run_id, call_id, "started")
        return ToolOutcome(
            message=message,
            approved_by=approval_source if approved else None,
            diagnosis_submitted=True,
            diagnosis=diagnosis if not exhausted else None,
            diagnosis_raw=raw,
            diagnosis_correct=correct,
            diagnosis_attempts=attempt,
            diagnosis_first_attempt_correct=first_attempt_correct,
            approval_token=approval_token,
            approval_source=approval_source,
        )


class Repair:
    def __init__(
        self,
        state: AgentState,
        registry: Any,
        store: Any = None,
        log: Callable[[dict[str, Any]], int] | None = None,
        checkpoint: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.state = state
        self.registry = registry
        self.store = store
        self.log = log
        self.checkpoint = checkpoint

    def execute(self, tool: Tool, args: dict[str, Any], call_id: str) -> ToolOutcome:
        safe_args = {key: value for key, value in args.items() if key != "approval_token"}
        canonical = normalize_repair_name(tool.name)
        reason = None
        if not self.state.diagnosis_submitted:
            reason = "BLOCKED: diagnosis required first"
        elif self.state.approval_token is None:
            reason = (
                "BLOCKED: approval not granted"
                if self.state.diagnosis_correct
                else "BLOCKED: diagnosis rejected; no gate receipt"
            )
        elif (
            args.get("approval_token") not in (None, "", "[REDACTED]")
            and args.get("approval_token") != self.state.approval_token
        ):
            reason = "BLOCKED: missing or invalid approval_token"
        elif canonical in {normalize_repair_name(name) for name in self.state.scenario.get("forbidden_repairs", [])}:
            reason = "BLOCKED: forbidden repair for this scenario"
        elif canonical not in {normalize_repair_name(name) for name in self.state.scenario.get("acceptable_repairs", [])}:
            reason = "BLOCKED: repair not acceptable for this scenario"
        if reason:
            violation = f"{canonical}: {reason}"
            forbidden_reason = reason if "forbidden" in reason else None
            if self.store is not None:
                self.store.add_policy_violation(self.state.run_id, tool.name, reason)
            return ToolOutcome(
                message=reason + SystemPrompt.BLOCKED_REPAIR_FOLLOWUP,
                status="denied",
                call_error=reason,
                policy_violation=violation,
                forbidden_reason=forbidden_reason,
            )
        approved_by = self.state.approval_source or ("operator" if self.state.approve == "prompt" else "policy")
        if self.store is not None:
            self.store.update_tool_call_status(self.state.run_id, call_id, "started")
        if self.checkpoint is not None:
            self.checkpoint()
        try:
            result = tool.invoke_handler(args)
            if isinstance(result, ToolOutcome):
                result = result.message
            if result.startswith("timeout waiting for"):
                result += SystemPrompt.REPAIR_TIMEOUT_FOLLOWUP
        except Exception as e:
            result = f"ERROR: {e}"
        status = "finished" if not result.startswith("ERROR:") else "failed"
        return ToolOutcome(
            message=result,
            status=status,
            approved_by=approved_by,
            repair=canonical if status == "finished" else None,
            repair_args=safe_args if status == "finished" else None,
            artifact_kind="repair_output_full",
            artifact_payload={"tool": canonical, "args": safe_args, "result": result},
        )
