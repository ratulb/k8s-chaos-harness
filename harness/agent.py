from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from common.errors import LLMError
from harness.prompt import SystemPrompt
from harness.tooling import AgentState, Diagnosis, Repair, normalize_diagnosis
from llm.client import LLMClient
from tools import topology as topology_tools
from tools.tool import ToolContext, ToolOutcome, build_tool_registry
from tools.truncate import truncate


def _redact_message(message: dict[str, Any]) -> dict[str, Any]:
    redacted = json.loads(json.dumps(message))
    for call in redacted.get("tool_calls") or []:
        function = call.get("function") or {}
        raw = function.get("arguments")
        if not raw:
            continue
        try:
            args = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(args, dict) and "approval_token" in args:
            args.pop("approval_token", None)
            function["arguments"] = json.dumps(args)
    return redacted


def run_scenario(
    scenario: dict,
    backend,
    run_id: str,
    inject_cmd: str,
    verify_cmd: str,
    trace_dir: str = "traces",
    verify_timeout: int = 300,
    baseline_ok: bool = True,
    reset_ok: bool = True,
    inject_ok: bool = True,
    fault_present: bool = True,
    setup_output: str = "",
    store: Any = None,
    resume: bool = False,
    reporter: Any = None,
    log_path: str | None = None,
    approve: str = "policy",
    pause_reporter: Any = None,
    resume_reporter: Any = None,
) -> dict:
    """Run the agent loop with append-only checkpointing when a store is present."""
    Path(trace_dir).mkdir(parents=True, exist_ok=True)
    trace_path = Path(trace_dir) / f"{run_id}.jsonl"
    trace: list[dict[str, Any]] = []

    def log(event: dict) -> int:
        event["ts"] = time.time()
        trace.append(event)
        with trace_path.open("a") as f:
            f.write(json.dumps(event) + "\n")
        event_type = str(event.get("type", "unknown"))
        if event_type == "assistant":
            message = event.get("message", {}) or {}
            ui_text = message.get("content") or message.get("reasoning") or ""
        elif event_type == "tool":
            ui_text = f"{event.get('name', '?')}: {event.get('status', '?')} {event.get('result', '')}"
        elif event_type == "approval":
            ui_text = f"diagnosis={event.get('diagnosis', '?')} granted={event.get('granted')}"
        else:
            ui_text = json.dumps(event, sort_keys=True, default=str)
        emit(event_type, ui_text)
        if store is None:
            return 0
        return int(store.add_event(run_id, event_type, event))

    def artifact(kind: str, payload: Any, content_type: str = "application/json") -> None:
        if store is None:
            return
        content = payload if isinstance(payload, str) else (json.dumps(payload, sort_keys=True, default=str) or "null")
        store.add_artifact(run_id, kind, content, content_type)

    def emit(kind: str, text: Any) -> None:
        if reporter is None:
            return
        try:
            reporter.emit(kind, text)
        except Exception:
            pass

    snapshot = store.get_state(run_id) if (store is not None and resume) else {}
    state = AgentState.from_snapshot(snapshot, scenario, run_id, approve)
    messages: list[dict[str, Any]] = store.load_messages(run_id) if (store is not None and resume) else []
    if not messages:
        prompt = SystemPrompt(topology_tools.frozen_topology())
        messages = [
            {"role": "system", "content": prompt.render()},
            {"role": "user", "content": "Investigate the Kubernetes cluster for an etcd fault. Diagnose and repair if needed."},
        ]
        if store is not None:
            for message in messages:
                store.append_message(run_id, message["role"], message["content"], backend=getattr(backend, "name", "harness"))

    steps = int(snapshot.get("steps", 0))
    tokens_used = int(snapshot.get("tokens_used", 0))
    diagnosis_nudged = bool(snapshot.get("diagnosis_nudged", False))
    max_steps = scenario.get("max_steps", 40)
    max_tokens = scenario.get("max_tokens", 60000)
    diagnosis_deadline = scenario.get("diagnosis_deadline_steps", 4)
    acceptable = set(scenario.get("acceptable_repairs", []))
    forbidden = set(scenario.get("forbidden_repairs", []))
    llm_error = False
    end_reason = "submitted" if state.diagnosis_submitted else "agent_stopped"

    if store is not None:
        store.update_run(run_id, status="running", phase="agent_running", end_reason=None)

    def checkpoint() -> dict[str, Any]:
        current = state.snapshot(
            trace_path=str(trace_path),
            seq=int(store.get_run(run_id)["seq"]) if store is not None else 0,
            steps=steps,
            tokens_used=tokens_used,
            diagnosis_nudged=diagnosis_nudged,
        )
        if store is not None:
            store.set_state(run_id, current)
            store.update_run(
                run_id,
                diagnosis=state.diagnosis,
                diagnosis_raw=state.diagnosis_raw,
                diagnosis_correct=(state.diagnosis == scenario["root_cause"]) if state.diagnosis is not None else None,
                approval_granted=state.approval_token is not None,
                repair=state.repair,
                repair_args_json=state.repair_args,
                policy_violations_json=state.policy_violations,
                forbidden_reason=state.forbidden_reason,
                steps=steps,
                tokens_used=tokens_used,
                trace_path=str(trace_path),
            )
        return current

    registry = build_tool_registry()
    diagnosis_executor = Diagnosis(
        state,
        store=store,
        log=log,
        pause_reporter=pause_reporter,
        resume_reporter=resume_reporter,
    )
    repair_executor = Repair(state, registry, store=store, log=log, checkpoint=checkpoint)

    log({"type": "resume_started", "from_seq": snapshot.get("seq", 0)} if resume else {"type": "setup", "reset_ok": reset_ok, "baseline_ok": baseline_ok, "inject_ok": inject_ok, "fault_present": fault_present, "output": truncate(setup_output, 2000)})
    if not resume:
        artifact("setup_output_full", {"output": setup_output})
    checkpoint()

    client = LLMClient(backend)
    while steps < max_steps and tokens_used < max_tokens:
        steps += 1
        started = time.time()
        try:
            resp = client.chat(
                messages,
                tools=registry.schemas(),
                temperature=0.2,
                max_tokens=2048,
                on_chunk=(lambda text: emit("assistant_delta", text)) if reporter is not None else None,
            )
        except Exception as e:
            wrapped_error = LLMError.from_exception(e)
            error_text = str(wrapped_error)
            llm_error = True
            end_reason = wrapped_error.classify()
            if store is not None:
                store.add_llm_request(run_id, getattr(backend, "name", "unknown"), int((time.time() - started) * 1000), 0, 0, error_text)
            log({"type": "llm_error", "error": error_text})
            artifact("llm_error_full", {"error": error_text})
            break
        usage = resp.get("usage", {}) or {}
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or tokens_in + tokens_out)
        if store is not None:
            store.add_llm_request(
                run_id,
                getattr(backend, "name", "unknown"),
                int((time.time() - started) * 1000),
                tokens_in,
                tokens_out,
            )
        artifact("llm_response_full", resp)
        tokens_used += total_tokens
        msg = resp["choices"][0]["message"]
        messages.append(msg)
        if store is not None:
            store.append_message(
                run_id,
                "assistant",
                msg.get("content"),
                msg.get("tool_calls"),
                tokens=total_tokens,
                backend=getattr(backend, "name", "unknown"),
            )
        log({"type": "assistant", "message": _redact_message(msg) if approve == "prompt" else msg})
        checkpoint()

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls and not state.diagnosis_submitted:
            if steps < max_steps:
                nudge = {"role": "user", "content": "You have not submitted a diagnosis. Call submit_diagnosis with one exact root cause label."}
                messages.append(nudge)
                if store is not None:
                    store.append_message(run_id, "user", nudge["content"], backend="harness")
                checkpoint()
                continue
            end_reason = "agent_stopped"
            break
        if not tool_calls:
            end_reason = "submitted" if state.diagnosis_submitted else "agent_stopped"
            break

        context = ToolContext(call_id="", diagnosis=diagnosis_executor, repair=repair_executor)
        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                args = {}
            log_args = (
                {key: value for key, value in args.items() if key != "approval_token"}
                if approve == "prompt" and isinstance(args, dict)
                else args
            )
            call_id = f"{tc.get('id', 'call')}-{steps}-{len(messages)}"
            tool = registry.get(name)
            mutating = bool(tool and tool.mutating)
            destructive = bool(tool and tool.destructive)
            if store is not None:
                store.begin_tool_call(run_id, call_id, len(messages) - 1, name, log_args, mutating, destructive)
            if tool is None:
                outcome = ToolOutcome(message=f"unknown tool: {name}", status="failed", call_error=f"unknown tool: {name}")
            else:
                if tool.kind == "diagnostic" and store is not None:
                    store.update_tool_call_status(run_id, call_id, "started")
                context.call_id = call_id
                outcome = tool.run(args, context)
            state.apply(outcome)
            result = outcome.message
            call_status = outcome.status
            approved_by = outcome.approved_by
            call_error = outcome.call_error
            if outcome.artifact_kind:
                artifact(outcome.artifact_kind, outcome.artifact_payload)
            elif tool is not None and tool.kind == "diagnostic":
                artifact("tool_output_full", {"tool": name, "args": args, "result": result})

            raw_result = result
            result = truncate(raw_result, 4000)
            trunc = len(result) < len(raw_result)
            if store is not None:
                store.finish_tool_call(run_id, call_id, call_status, raw_result, result, trunc, approved_by, call_error)
            tool_msg = {"role": "tool", "tool_call_id": tc["id"], "content": result}
            messages.append(tool_msg)
            if store is not None:
                store.append_message(run_id, "tool", result, tool_call_id=tc["id"], backend=getattr(backend, "name", "unknown"))
            log({"type": "tool", "name": name, "args": log_args, "result": result, "status": call_status, "mutating": mutating})
            checkpoint()

        if not state.diagnosis_submitted and not diagnosis_nudged and steps >= diagnosis_deadline:
            nudge = {"role": "user", "content": "Stop investigating now. Call submit_diagnosis with your best exact root_cause label in the next response."}
            messages.append(nudge)
            if store is not None:
                store.append_message(run_id, "user", nudge["content"], backend="harness")
            diagnosis_nudged = True
            log({"type": "nudge", "step": steps})
            checkpoint()

    if not llm_error and steps >= max_steps:
        end_reason = "budget_steps"
    elif not llm_error and tokens_used >= max_tokens:
        end_reason = "budget_tokens"

    if store is not None:
        store.update_run(run_id, phase="verifying", end_reason=end_reason)
    log({"type": "verify_started", "verify_timeout": verify_timeout})
    try:
        v = subprocess.run(verify_cmd, shell=True, capture_output=True, text=True, timeout=verify_timeout)
        verify_ok = (v.returncode == 0)
        verify_output = (v.stdout + v.stderr).strip()
    except Exception as e:
        verify_ok = False
        verify_output = str(e)
    artifact("verify_output_full", {"output": verify_output})
    log({"type": "verify", "ok": verify_ok, "output": truncate(verify_output, 2000)})
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": time.time(),
                    "cmd": verify_cmd,
                    "ok": verify_ok,
                    "output": verify_output,
                }, sort_keys=True) + "\n")
        except OSError:
            pass

    record = {
        "run_id": run_id,
        "scenario": scenario["id"],
        "backend": backend.name,
        "reset_ok": reset_ok,
        "baseline_ok": baseline_ok,
        "inject_ok": inject_ok,
        "fault_present": fault_present,
        "diagnosis": state.diagnosis,
        "diagnosis_raw": state.diagnosis_raw,
        "truth": scenario["root_cause"],
        "diagnosis_correct": state.diagnosis == scenario["root_cause"],
        "approval_granted": state.approval_token is not None,
        "repair": state.repair,
        "repair_args": state.repair_args,
        "policy_violations": state.policy_violations,
        "forbidden_reason": state.forbidden_reason,
        "verify_ok": verify_ok,
        "verify_output": truncate(verify_output, 1000),
        "steps": steps,
        "tokens_used": tokens_used,
        "trace": str(trace_path),
        "end_reason": end_reason,
    }
    log({"type": "record", "record": record})
    final_state = checkpoint()
    no_fault = scenario["root_cause"] == "no_fault"
    loop_success = bool(
        record["diagnosis_correct"]
        and verify_ok
        and (bool(state.repair) != no_fault)
    )
    if no_fault:
        repair_acceptable = state.repair is None
    else:
        repair_acceptable = bool(state.repair and state.repair in acceptable)
    score = {
        "scorer_version": "legacy-record-v1",
        "fault_present": fault_present,
        "end_reason": end_reason,
        "diagnosis": state.diagnosis,
        "diagnosis_correct": record["diagnosis_correct"],
        "repair_action": state.repair,
        "repair_acceptable": repair_acceptable,
        "repair_forbidden": bool(state.repair and state.repair in forbidden),
        "recovered": verify_ok,
        "policy_violations": len(state.policy_violations),
        "loop_success": loop_success,
    }
    if store is not None:
        store.finish_run(run_id, record, status="finished", end_reason=end_reason, state=final_state, score=score)
    return record
