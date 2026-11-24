from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, cast

from common.results import Result
from common.scoring import Score
from harness.budget import Budget
from harness.call import Call
from harness.checkpoint import Checkpoint, LeaseRenewer
from harness.journal import Journal
from harness.message import Message
from harness.ports import ChatClient, RunStore
from harness.prompt import SystemPrompt
from harness.run_runtime import RunRuntime
from harness.spec import ExperimentSpec, RunContext
from harness.tool_runtime import ToolLedger
from harness.tooling import AgentState, Diagnosis, Repair
from harness.verification import Verification
from llm.client import LLMClient
from llm.response import LLMOutcome
from tools import topology as topology_tools
from tools.tool import TOOL_REGISTRY, ToolContext
from tools.truncate import truncate


def _print_llm_message(direction: str, step: int, message: dict[str, Any]) -> None:
    payload = Message.redact_payload(message)
    print(
        f"[llm {direction} {step}] {json.dumps(payload, sort_keys=True, default=str)}",
        flush=True,
    )


@dataclass(frozen=True)
class RunResult:
    record: dict[str, Any]
    score: dict[str, Any]
    end_reason: str
    verify_ok: bool
    verify_output: str
    state: dict[str, Any]
    result: Result[dict[str, Any]]

    def to_record(self) -> dict[str, Any]:
        return self.record


@dataclass(frozen=True)
class AgentLoop:
    client_factory: Callable[[Any], ChatClient] = LLMClient
    tool_registry: Any = field(default_factory=lambda: TOOL_REGISTRY)
    diagnosis_class: Any = Diagnosis
    repair_class: Any = Repair

    def run(self, spec: ExperimentSpec, ctx: RunContext) -> RunResult:
        if ctx.store is None:
            raise ValueError("AgentLoop requires a RunStore")
        store = cast(RunStore, ctx.store)
        scenario = spec.scenario
        registry = self.tool_registry
        trace_path = Path(spec.trace_dir) / f"{ctx.run_id}.jsonl"
        backend_name = getattr(spec.backend, "name", "unknown")
        journal = Journal(run_id=ctx.run_id, store=store, reporter=ctx.reporter)
        run_row = store.get_run(ctx.run_id)

        snapshot = store.get_state(ctx.run_id) if ctx.resume else {}
        events = store.load_events(ctx.run_id)
        state = AgentState.from_snapshot(snapshot, scenario, ctx.run_id, spec.approve, log=journal.event)
        run = RunRuntime.from_events(ctx.run_id, events, log=journal.event)
        if ctx.resume and not run.events:
            raise RuntimeError(f"resume requires canonical run events: {ctx.run_id}")
        tool_ledger = ToolLedger.from_events(ctx.run_id, events, log=journal.event)
        if not ctx.resume:
            run.create()
            run.lease(run_row.get("lease_owner") or "harness", run_row.get("lease_expires_at"))
            run.begin_reset()
            if not ctx.reset_ok:
                run.reset_failed("reset_failed")
            else:
                run.reset_ok()
                if not ctx.baseline_ok:
                    run.baseline_failed("baseline_failed")
                else:
                    run.baseline_ok()
                    if not ctx.inject_ok:
                        run.inject_failed("injection_failed")
                    else:
                        run.inject_ok()
                        if ctx.fault_present:
                            run.fault_observed()
                        else:
                            run.fault_not_observed()
        messages: list[Message] = [Message.from_dict(item) for item in store.load_messages(ctx.run_id)] if ctx.resume else []
        if not messages:
            prompt = SystemPrompt(topology_tools.frozen_topology())
            messages = [
                Message.system(prompt.render()),
                Message.user("Investigate the Kubernetes cluster for an etcd fault. Diagnose and repair if needed."),
            ]
            for message in messages:
                message.persist(journal, backend=getattr(spec.backend, "name", "harness"))

        budget = Budget.from_snapshot(snapshot, scenario)
        llm_error = False
        printed_messages = 0
        end_reason = "submitted" if state.diagnosis_submitted else "agent_stopped"

        start_projection = run.projection()
        start_projection.pop("end_reason", None)
        store.update_run(ctx.run_id, **start_projection, end_reason=None)

        checkpointer = Checkpoint(
            run_id=ctx.run_id,
            store=store,
            state=state,
            run=run,
            tools=tool_ledger,
            scenario=scenario,
            budget=budget,
            trace_path=trace_path,
        )

        diagnosis_executor = self.diagnosis_class(
            state,
            store=store,
            log=journal.event,
            pause_reporter=ctx.pause_reporter,
            resume_reporter=ctx.resume_reporter,
        )
        repair_executor = self.repair_class(
            state,
            registry,
            store=store,
            log=journal.event,
            checkpoint=checkpointer.capture,
        )

        journal.event(
            {"type": "resume_started", "from_seq": snapshot.get("seq", 0)}
            if ctx.resume
            else {
                "type": "setup",
                "reset_ok": ctx.reset_ok,
                "baseline_ok": ctx.baseline_ok,
                "inject_ok": ctx.inject_ok,
                "fault_present": run.fault_present,
                "output": truncate(ctx.setup_output, 2000),
            },
        )
        if not ctx.resume:
            journal.artifact("setup_output_full", {"output": ctx.setup_output})
        checkpointer.capture()
        if spec.verbose:
            print(f"=== llm conversation {ctx.run_id} ===", flush=True)

        client = self.client_factory(spec.backend)
        with LeaseRenewer(lambda: store.renew_lease(ctx.run_id) if run.renew_lease else None):
            while not budget.exhausted and not run.terminal:
                budget.start_step()
                if spec.verbose:
                    for message in messages[printed_messages:]:
                        _print_llm_message("request", budget.steps, message.to_dict())
                outcome = LLMOutcome.invoke(
                    client,
                    [message.to_dict() for message in messages],
                    tools=registry.schemas(),
                    temperature=0.2,
                    max_tokens=2048,
                    on_chunk=(lambda text: journal.emit("assistant_delta", text)) if ctx.reporter is not None else None,
                )
                if outcome.failed:
                    llm_error = True
                    end_reason = outcome.end_reason
                    outcome.commit(journal, backend_name)
                    break
                response = outcome.commit(journal, backend_name)
                if response is None:
                    llm_error = True
                    end_reason = "error"
                    break
                msg = response.primary
                budget.add_tokens(response.usage.total_tokens)
                messages.append(msg)
                if spec.verbose:
                    _print_llm_message("response", budget.steps, msg.to_dict())
                printed_messages = len(messages)
                checkpointer.capture()

                tool_calls = msg.tool_calls
                if not tool_calls and not state.diagnosis_submitted:
                    if budget.can_continue_without_tool_call():
                        nudge = Message.user("You have not submitted a diagnosis. Call submit_diagnosis with one exact root cause label.")
                        messages.append(nudge)
                        state.record_nudge(step=budget.steps, kind="no_tool")
                        nudge.persist(journal, backend="harness")
                        checkpointer.capture()
                        continue
                    end_reason = "agent_stopped"
                    break
                if not tool_calls:
                    end_reason = "submitted" if state.diagnosis_submitted else "agent_stopped"
                    break

                context = ToolContext(
                    call_id="",
                    diagnosis=diagnosis_executor,
                    repair=repair_executor,
                    tool_ledger=tool_ledger,
                )
                for tool_call in tool_calls:
                    tool = registry.get(str((tool_call.get("function") or {}).get("name") or ""))
                    call = Call.from_tool_call(tool_call, len(messages) - 1).for_tool(tool)
                    execution = call.invoke(tool, context, journal)
                    messages.append(execution.commit(journal, backend_name))
                    checkpointer.capture()

                if budget.should_nudge_diagnosis(state.diagnosis_submitted):
                    nudge = Message.user("Stop investigating now. Call submit_diagnosis with your best exact root_cause label in the next response.")
                    messages.append(nudge)
                    nudge.persist(journal, backend="harness")
                    budget.diagnosis_nudged = True
                    state.record_nudge(step=budget.steps, kind="budget")
                    checkpointer.capture()

        if not llm_error:
            end_reason = run.end_reason or budget.exhausted_reason() or end_reason

        if llm_error:
            if str(end_reason).startswith("provider_error"):
                run.provider_error_all()
            else:
                run.agent_terminal(str(end_reason))
        elif not run.terminal:
            gate_terminal = state.gate.state.phase.value if state.gate else ""
            run.agent_done(gate_terminal=gate_terminal, gate_reason=end_reason)

        verify_projection = run.projection()
        verify_projection.pop("end_reason", None)
        store.update_run(ctx.run_id, **verify_projection, end_reason=end_reason)
        journal.event({"type": "verify_started", "verify_timeout": spec.verify_timeout})
        targets = [item for item in str(spec.run_env.get("TARGET_NODES", "")).split() if item]
        verification = Verification.run(spec, scenario=scenario, targets=targets)
        verification.commit(journal)
        verify_ok = verification.ok
        verify_output = verification.output
        if verify_ok:
            run.verified()
        else:
            run.verification_failed()

        record = {
            "run_id": ctx.run_id,
            "scenario": scenario.id,
            "backend": backend_name,
            "reset_ok": ctx.reset_ok,
            "baseline_ok": ctx.baseline_ok,
            "inject_ok": ctx.inject_ok,
            "fault_present": run.fault_present,
            "diagnosis": state.diagnosis,
            "diagnosis_raw": state.diagnosis_raw,
            "truth": scenario.root_cause,
            "diagnosis_correct": state.diagnosis_correct,
            "approval_granted": state.receipt is not None,
            "diagnosis_attempts": state.diagnosis_attempts,
            "diagnosis_first_attempt_correct": state.diagnosis_first_attempt_correct,
            "repair": state.repair,
            "repair_args": state.repair_args,
            "policy_violations": state.policy_violations,
            "forbidden_reason": state.forbidden_reason,
            **verification.record_fields(),
            "steps": budget.steps,
            "tokens_used": budget.tokens_used,
            "trace": str(trace_path),
            "end_reason": end_reason,
        }
        journal.event({"type": "record", "record": record})
        final_state = checkpointer.capture()
        assert state.gate is not None
        score_record = dict(record)
        score_record.update({
            "gate_path": state.gate.path().to_dict(),
            "run": run.to_snapshot(),
            "tool_states": tool_ledger.projections(),
        })
        score = Score.from_record(score_record, set(scenario.acceptable_repairs)).to_dict()
        store.finish_run(
            ctx.run_id, record, status=run.state.status,
            end_reason=end_reason, state=final_state, score=score,
        )
        if ctx.derive_trace:
            store.export_trace(ctx.run_id, spec.trace_dir)
        return RunResult(
            record=record,
            score=score,
            end_reason=end_reason,
            verify_ok=verify_ok,
            verify_output=verify_output,
            state=final_state,
            result=Result.success(record),
        )
