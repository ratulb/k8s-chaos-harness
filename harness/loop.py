from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, cast

from common.errors import LLMError
from common.results import Result
from common.scoring import Score
from harness.budget import Budget
from harness.call import Call
from harness.checkpoint import Checkpoint
from harness.message import Message
from harness.ports import ChatClient, RunStore
from harness.prompt import SystemPrompt
from harness.spec import ExperimentSpec, RunContext
from harness.tooling import AgentState, Diagnosis, Repair
from llm.client import LLMClient
from llm.response import LLMResponse
from tools import topology as topology_tools
from tools.tool import TOOL_REGISTRY, ToolContext, ToolOutcome
from tools.truncate import truncate


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
            raise ValueError("AgentLoop requires a RunStore; use the legacy facade for detached runs")
        store = cast(RunStore, ctx.store)
        scenario = spec.scenario
        registry = self.tool_registry
        trace_path = Path(spec.trace_dir) / f"{ctx.run_id}.jsonl"

        def log(event: dict[str, Any]) -> int:
            event["ts"] = time.time()
            trace_event = Message.redact_payload(event)
            event_type = str(event.get("type", "unknown"))
            if event_type == "assistant":
                message = trace_event.get("message") or {}
                ui_text = message.get("content") or message.get("reasoning") or ""
            elif event_type == "tool":
                ui_text = f"{trace_event.get('name', '?')}: {trace_event.get('status', '?')} {trace_event.get('result', '')}"
            elif event_type == "approval":
                ui_text = f"diagnosis={trace_event.get('diagnosis', '?')} granted={trace_event.get('granted')}"
            else:
                ui_text = str(trace_event)
            emit(event_type, ui_text)
            return int(store.add_event(ctx.run_id, event_type, event))

        def artifact(kind: str, payload: Any, content_type: str = "application/json") -> None:
            content = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True, default=str)
            store.add_artifact(ctx.run_id, kind, content, content_type)

        def emit(kind: str, text: Any) -> None:
            if ctx.reporter is None:
                return
            try:
                ctx.reporter.emit(kind, text)
            except Exception:
                pass

        snapshot = store.get_state(ctx.run_id) if ctx.resume else {}
        state = AgentState.from_snapshot(snapshot, scenario, ctx.run_id, spec.approve)
        messages: list[Message] = [Message.from_dict(item) for item in store.load_messages(ctx.run_id)] if ctx.resume else []
        if not messages:
            prompt = SystemPrompt(topology_tools.frozen_topology())
            messages = [
                Message.system(prompt.render()),
                Message.user("Investigate the Kubernetes cluster for an etcd fault. Diagnose and repair if needed."),
            ]
            for message in messages:
                store.append_message(ctx.run_id, message.role, message.content, backend=getattr(spec.backend, "name", "harness"))

        budget = Budget.from_snapshot(snapshot, scenario)
        llm_error = False
        end_reason = "submitted" if state.diagnosis_submitted else "agent_stopped"

        store.update_run(ctx.run_id, status="running", phase="agent_running", end_reason=None)

        checkpointer = Checkpoint(
            run_id=ctx.run_id,
            store=store,
            state=state,
            scenario=scenario,
            budget=budget,
            trace_path=trace_path,
        )

        diagnosis_executor = self.diagnosis_class(
            state,
            store=store,
            log=log,
            pause_reporter=ctx.pause_reporter,
            resume_reporter=ctx.resume_reporter,
        )
        repair_executor = self.repair_class(state, registry, store=store, log=log, checkpoint=checkpointer.capture)

        log(
            {"type": "resume_started", "from_seq": snapshot.get("seq", 0)}
            if ctx.resume
            else {
                "type": "setup",
                "reset_ok": ctx.reset_ok,
                "baseline_ok": ctx.baseline_ok,
                "inject_ok": ctx.inject_ok,
                "fault_present": ctx.fault_present,
                "output": truncate(ctx.setup_output, 2000),
            },
        )
        if not ctx.resume:
            artifact("setup_output_full", {"output": ctx.setup_output})
        checkpointer.capture()

        client = self.client_factory(spec.backend)
        while not budget.exhausted:
            budget.start_step()
            started = time.time()
            try:
                response = LLMResponse.from_value(client.chat(
                    [message.to_dict() for message in messages],
                    tools=registry.schemas(),
                    temperature=0.2,
                    max_tokens=2048,
                    on_chunk=(lambda text: emit("assistant_delta", text)) if ctx.reporter is not None else None,
                ))
            except Exception as exc:
                error = LLMError.from_exception(exc)
                error_text = str(error)
                llm_error = True
                end_reason = error.classify()
                store.add_llm_request(
                    ctx.run_id, getattr(spec.backend, "name", "unknown"),
                    int((time.time() - started) * 1000), 0, 0, error_text,
                )
                log({"type": "llm_error", "error": error_text})
                artifact("llm_error_full", {"error": error_text})
                break
            usage = response.usage
            store.add_llm_request(
                ctx.run_id,
                getattr(spec.backend, "name", "unknown"),
                int((time.time() - started) * 1000),
                usage.prompt_tokens,
                usage.completion_tokens,
            )
            artifact("llm_response_full", Message.redact_payload(response.to_dict()))
            budget.add_tokens(usage.total_tokens)
            msg = response.messages[0]
            messages.append(msg)
            stored_msg = Message.redact_payload(msg.to_dict())
            store.append_message(
                ctx.run_id,
                stored_msg["role"],
                stored_msg.get("content"),
                stored_msg.get("tool_calls", ()),
                tokens=usage.total_tokens,
                backend=getattr(spec.backend, "name", "unknown"),
            )
            log({
                "type": "assistant",
                "message": stored_msg,
                "finish_reason": response.finish_reason,
                "stream_fallback": response.stream_fallback,
            })
            checkpointer.capture()

            tool_calls = msg.tool_calls
            if not tool_calls and not state.diagnosis_submitted:
                if budget.can_continue_without_tool_call():
                    nudge = Message.user("You have not submitted a diagnosis. Call submit_diagnosis with one exact root cause label.")
                    messages.append(nudge)
                    store.append_message(ctx.run_id, nudge.role, nudge.content, backend="harness")
                    checkpointer.capture()
                    continue
                end_reason = "agent_stopped"
                break
            if not tool_calls:
                end_reason = "submitted" if state.diagnosis_submitted else "agent_stopped"
                break

            context = ToolContext(call_id="", diagnosis=diagnosis_executor, repair=repair_executor)
            for tool_call in tool_calls:
                call = Call.from_tool_call(tool_call, len(messages) - 1)
                tool = registry.get(call.name)
                call = replace(call, mutating=bool(tool and tool.mutating), destructive=bool(tool and tool.destructive))
                store.begin_tool_call(
                    ctx.run_id, call.id, call.msg_seq, call.name,
                    Message.redact_payload(call.args), call.mutating, call.destructive,
                )
                if tool is None:
                    outcome = ToolOutcome(
                        message=f"unknown tool: {call.name}",
                        status="failed",
                        call_error=f"unknown tool: {call.name}",
                    )
                else:
                    if tool.kind == "diagnostic":
                        store.update_tool_call_status(ctx.run_id, call.id, "started")
                    context.call_id = call.id
                    context.call = call
                    outcome = tool.run(call.args, context)
                state.apply(outcome)
                raw_result = outcome.message
                if outcome.artifact_kind:
                    artifact(outcome.artifact_kind, outcome.artifact_payload)
                elif tool is not None and tool.kind == "diagnostic":
                    artifact("tool_output_full", {
                        "tool": call.name,
                        "args": Message.redact_payload(call.args),
                        "result": Message.redact_payload(raw_result),
                    })
                tool_message = Message.from_dict(Message.truncate(Message.tool(raw_result, str(tool_call.get("id") or call.id)).to_dict(), 4000))
                sent_result = str(tool_message.content or "")
                store.finish_tool_call(
                    ctx.run_id, call.id, outcome.status,
                    Message.redact_payload(raw_result),
                    Message.redact_payload(sent_result),
                    len(sent_result) < len(raw_result),
                    outcome.approved_by,
                    outcome.call_error,
                )
                messages.append(tool_message)
                store.append_message(
                    ctx.run_id, tool_message.role,
                    Message.redact_payload(tool_message.content),
                    tool_call_id=tool_message.tool_call_id,
                    backend=getattr(spec.backend, "name", "unknown"),
                )
                log({
                    "type": "tool",
                    "name": call.name,
                    "args": Message.redact_payload(call.args),
                    "result": Message.redact_payload(sent_result),
                    "status": outcome.status,
                    "mutating": call.mutating,
                })
                checkpointer.capture()

            if budget.should_nudge_diagnosis(state.diagnosis_submitted):
                nudge = Message.user("Stop investigating now. Call submit_diagnosis with your best exact root_cause label in the next response.")
                messages.append(nudge)
                store.append_message(ctx.run_id, nudge.role, nudge.content, backend="harness")
                budget.diagnosis_nudged = True
                log({"type": "nudge", "step": budget.steps})
                checkpointer.capture()

        if not llm_error:
            end_reason = budget.exhausted_reason() or end_reason

        store.update_run(ctx.run_id, phase="verifying", end_reason=end_reason)
        log({"type": "verify_started", "verify_timeout": spec.verify_timeout})
        try:
            completed = subprocess.run(
                spec.verify_cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=spec.verify_timeout,
                env=dict(spec.run_env),
            )
            verify_ok = completed.returncode == 0
            verify_output = (completed.stdout + completed.stderr).strip()
        except Exception as exc:
            verify_ok = False
            verify_output = str(exc)
        artifact("verify_output_full", {"output": verify_output})
        log({"type": "verify", "ok": verify_ok, "output": truncate(verify_output, 2000)})

        record = {
            "run_id": ctx.run_id,
            "scenario": scenario.id,
            "backend": getattr(spec.backend, "name", "unknown"),
            "reset_ok": ctx.reset_ok,
            "baseline_ok": ctx.baseline_ok,
            "inject_ok": ctx.inject_ok,
            "fault_present": ctx.fault_present,
            "diagnosis": state.diagnosis,
            "diagnosis_raw": state.diagnosis_raw,
            "truth": scenario.root_cause,
            "diagnosis_correct": state.diagnosis == scenario.root_cause,
            "approval_granted": state.approval_token is not None,
            "diagnosis_attempts": state.diagnosis_attempts,
            "diagnosis_first_attempt_correct": state.diagnosis_first_attempt_correct,
            "repair": state.repair,
            "repair_args": state.repair_args,
            "policy_violations": state.policy_violations,
            "forbidden_reason": state.forbidden_reason,
            "verify_ok": verify_ok,
            "verify_output": truncate(verify_output, 1000),
            "steps": budget.steps,
            "tokens_used": budget.tokens_used,
            "trace": str(trace_path),
            "end_reason": end_reason,
        }
        log({"type": "record", "record": record})
        final_state = checkpointer.capture()
        score = Score.from_record(record, set(scenario.acceptable_repairs)).to_dict()
        store.finish_run(
            ctx.run_id, record, status="finished",
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
