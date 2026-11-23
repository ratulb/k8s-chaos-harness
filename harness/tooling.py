from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable

from common.results import Approval, Refusal, Result, ResultKind
from common.vocab import ROOT_CAUSE_LABELS
from harness.gate_runtime import GateRuntime
from harness.machines.gate import MAX_DIAGNOSIS_ATTEMPTS, GatePhase
from harness.prompt import SystemPrompt
from tools.naming import normalize_repair_name
from tools.scenario import Scenario, as_scenario
from tools.tool import Tool, ToolOutcome


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
    scenario: Scenario
    run_id: str
    approve: str
    gate: GateRuntime | None = None
    log: Callable[[dict[str, Any]], int] | None = None

    def __post_init__(self) -> None:
        self.scenario = as_scenario(self.scenario)
        if self.gate is None:
            self.gate = GateRuntime(run_id=self.run_id, log=self.log)

    @classmethod
    def from_snapshot(
        cls,
        snapshot: dict[str, Any],
        scenario: Scenario,
        run_id: str,
        approve: str,
        log: Callable[[dict[str, Any]], int] | None = None,
    ) -> "AgentState":
        return cls(
            scenario=as_scenario(scenario),
            run_id=run_id,
            approve=approve,
            gate=GateRuntime.from_snapshot(run_id, snapshot.get("gate"), log=log),
            log=log,
        )

    def snapshot(
        self,
        trace_path: str,
        seq: int,
        steps: int,
        tokens_used: int,
        diagnosis_nudged: bool,
    ) -> dict[str, Any]:
        assert self.gate is not None
        return {
            "gate": self.gate.to_snapshot(),
            "steps": steps,
            "tokens_used": tokens_used,
            "diagnosis_nudged": self.gate.nudged,
            "trace_path": trace_path,
            "seq": seq,
        }

    @property
    def diagnosis(self) -> str | None:
        assert self.gate is not None
        return self.gate.state.diagnosis_label

    @property
    def diagnosis_raw(self) -> Any | None:
        assert self.gate is not None
        return self.gate.diagnosis_raw

    @property
    def diagnosis_submitted(self) -> bool:
        assert self.gate is not None
        return self.gate.state.diagnosis_attempts > 0

    @property
    def diagnosis_correct(self) -> bool:
        assert self.gate is not None
        return bool(self.gate.state.diagnosis_correct)

    @property
    def diagnosis_attempts(self) -> int:
        assert self.gate is not None
        return self.gate.state.diagnosis_attempts

    @property
    def diagnosis_first_attempt_correct(self) -> bool:
        assert self.gate is not None
        return self.gate.state.first_attempt_correct

    @property
    def receipt(self) -> str | None:
        assert self.gate is not None
        return self.gate.receipt

    @property
    def approval_source(self) -> str | None:
        assert self.gate is not None
        return self.gate.approval_source

    @property
    def repair(self) -> str | None:
        assert self.gate is not None
        return self.gate.accepted_repair

    @property
    def repair_args(self) -> dict[str, Any] | None:
        assert self.gate is not None
        return self.gate.repair_args

    @property
    def policy_violations(self) -> list[str]:
        assert self.gate is not None
        return self.gate.policy_violations

    @property
    def forbidden_reason(self) -> str | None:
        assert self.gate is not None
        return self.gate.forbidden_reason

    def record_nudge(self, *, step: int | None = None, kind: str = "budget") -> None:
        assert self.gate is not None
        self.gate.nudge(step=step, kind=kind)


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
        self.pause_reporter = pause_reporter
        self.resume_reporter = resume_reporter

    def decide(self, tool: Tool, args: dict[str, Any]) -> Result[Approval]:
        assert self.state.gate is not None
        gate = self.state.gate
        raw = args.get("root_cause")
        diagnosis = normalize_diagnosis(raw)
        attempt = gate.state.diagnosis_attempts + 1
        exhausted = gate.state.diagnosis_attempts >= MAX_DIAGNOSIS_ATTEMPTS
        correct = not exhausted and diagnosis == self.state.scenario.root_cause
        first_attempt_correct = correct and attempt == 1
        no_fault = diagnosis == "no_fault" or self.state.scenario.root_cause == "no_fault"
        approved = correct
        source = None
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
            source = "operator"
        elif approved:
            source = "policy"
        gate.submit_diagnosis(
            label=diagnosis,
            correct=correct,
            approved=approved,
            attempts=attempt,
            first_attempt_correct=first_attempt_correct,
            is_no_fault=no_fault,
            source=source,
            raw=raw,
        )
        if exhausted:
            reason = "diagnosis attempts exhausted; report findings and escalate without repair"
        elif not correct:
            reason = "diagnosis rejected; gather new evidence before resubmitting; do not brute-force labels"
        elif not approved:
            reason = "diagnosis correct; operator declined repair approval"
        else:
            reason = ""
        if self.store is not None:
            self.store.add_diagnosis_attempt(self.state.run_id, raw, diagnosis, correct)
        if reason:
            return Result.refusal(Refusal(
                reason=reason,
                message=reason,
                canonical="diagnosis",
                kind="diagnosis",
                diagnosis=diagnosis if not exhausted else None,
                diagnosis_correct=correct,
                first_attempt_correct=first_attempt_correct,
                attempt=attempt,
            ))
        return Result.approval(Approval(
            run_id=self.state.run_id,
            diagnosis=diagnosis,
            source=source or "policy",
            message="diagnosis accepted; gate approved",
            raw=raw,
            attempt=attempt,
            diagnosis_correct=correct,
            first_attempt_correct=first_attempt_correct,
        ))

    def execute(self, tool: Tool, args: dict[str, Any], call_id: str, runtime: Any = None) -> ToolOutcome:
        decision = self.decide(tool, args)
        raw = args.get("root_cause")
        if decision.kind is not ResultKind.APPROVAL:
            refusal = decision.unwrap()
            outcome = ToolOutcome(
                message=refusal.message,
                status="denied",
                result_code="denied",
                denied=True,
                call_error=refusal.message,
                violation_kind="diagnosis",
                diagnosis_submitted=True,
                diagnosis=refusal.diagnosis,
                diagnosis_raw=raw,
                diagnosis_correct=refusal.diagnosis_correct,
                diagnosis_attempts=refusal.attempt or self.state.diagnosis_attempts + 1,
                diagnosis_first_attempt_correct=refusal.first_attempt_correct,
            )
        else:
            approval = decision.unwrap()
            if runtime is not None:
                runtime.start()
            elif self.store is not None:
                self.store.update_tool_call_status(self.state.run_id, call_id, "started")
            outcome = ToolOutcome(
                message=approval.message,
                result_code="success",
                approved_by=approval.source,
                diagnosis_submitted=True,
                diagnosis=approval.diagnosis,
                diagnosis_raw=raw,
                diagnosis_correct=approval.diagnosis_correct,
                diagnosis_attempts=approval.attempt,
                diagnosis_first_attempt_correct=approval.first_attempt_correct,
            )
        if runtime is not None:
            runtime.apply_outcome(outcome)
            outcome = replace(outcome, status=runtime.status, result_code=runtime.result_code)
        return outcome


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
        self.checkpoint = checkpoint

    def authorize(self, tool: Tool, args: dict[str, Any]) -> Result[Approval]:
        assert self.state.gate is not None
        gate = self.state.gate
        canonical = normalize_repair_name(tool.name)
        phase = gate.state.phase
        if phase is GatePhase.ESCALATED:
            reason = "BLOCKED: gate escalated"
        elif phase is GatePhase.AWAIT_DIAGNOSIS and gate.state.diagnosis_attempts == 0:
            reason = "BLOCKED: diagnosis required first"
        elif gate.receipt is None:
            reason = (
                "BLOCKED: approval not granted"
                if gate.state.diagnosis_correct
                else "BLOCKED: diagnosis rejected; no gate receipt"
            )
        elif phase is GatePhase.CLEARED:
            reason = "BLOCKED: no fault detected; repair forbidden"
        elif canonical in self.state.scenario.forbidden_repairs:
            reason = "BLOCKED: forbidden repair for this scenario"
        elif canonical not in self.state.scenario.acceptable_repairs:
            reason = "BLOCKED: repair not acceptable for this scenario"
        else:
            reason = None
        gate.propose_repair(canonical, args, self.state.scenario, denial_reason=reason)
        if reason is not None:
            if self.store is not None:
                self.store.add_policy_violation(self.state.run_id, tool.name, reason)
            return Result.refusal(Refusal(
                reason=reason,
                message=reason + SystemPrompt.BLOCKED_REPAIR_FOLLOWUP,
                canonical=canonical,
                kind="forbidden" if "forbidden" in reason else "policy",
            ))
        return Result.approval(Approval(
            run_id=self.state.run_id,
            diagnosis=self.state.diagnosis,
            source=gate.approval_source or ("operator" if self.state.approve == "prompt" else "policy"),
            message="",
        ))

    def execute(self, tool: Tool, args: dict[str, Any], call_id: str, runtime: Any = None) -> ToolOutcome:
        assert self.state.gate is not None
        canonical = normalize_repair_name(tool.name)
        decision = self.authorize(tool, args)
        if decision.kind is not ResultKind.APPROVAL:
            refusal = decision.unwrap()
            outcome = ToolOutcome(
                message=refusal.message,
                status="denied",
                result_code="denied",
                denied=True,
                call_error=refusal.reason,
                policy_violation=f"{canonical}: {refusal.reason}",
                violation_kind="forbidden" if "forbidden" in refusal.reason else "policy",
                forbidden_reason=refusal.reason if "forbidden" in refusal.reason else None,
            )
            if runtime is not None:
                runtime.apply_outcome(outcome)
                outcome = replace(outcome, status=runtime.status, result_code=runtime.result_code)
            return outcome
        approval = decision.unwrap()
        if runtime is not None:
            runtime.start()
        elif self.store is not None:
            self.store.update_tool_call_status(self.state.run_id, call_id, "started")
        if self.checkpoint is not None:
            self.checkpoint()
        try:
            raw = tool.invoke_handler(args)
            if isinstance(raw, ToolOutcome):
                result = raw.message
                timed_out = raw.timed_out or result.startswith("timeout waiting for")
                unknown_effect = raw.unknown_effect
                error = raw.call_error
            else:
                result = str(raw)
                timed_out = result.startswith("timeout waiting for")
                unknown_effect = False
                error = None
            if timed_out:
                result += SystemPrompt.REPAIR_TIMEOUT_FOLLOWUP
        except Exception as e:
            result = f"ERROR: {e}"
            timed_out = False
            unknown_effect = False
            error = str(e)
        code = "timeout" if timed_out else "failed" if error else "unknown_effect" if unknown_effect else "success"
        if runtime is not None:
            if unknown_effect:
                runtime.cancel()
            else:
                runtime.failure(code, error) if code in {"timeout", "failed"} else runtime.success()
            status = runtime.status
            result_code = runtime.result_code
            self.state.gate.finish_repair(tool_state=runtime.state, idempotency_key=f"repair-result:{call_id}")
        else:
            status = "failed" if error else "finished"
            result_code = code
            self.state.gate.finish_repair(status=status, result=result, idempotency_key=f"repair-result:{call_id}")
        return ToolOutcome(
            message=result,
            status=status,
            result_code=result_code,
            timed_out=timed_out,
            unknown_effect=unknown_effect,
            call_error=error,
            approved_by=approval.source,
            repair=canonical if status == "finished" else None,
            repair_args=dict(args) if status == "finished" else None,
            artifact_kind="repair_output_full",
            artifact_payload={"tool": canonical, "args": dict(args), "result": result},
        )
