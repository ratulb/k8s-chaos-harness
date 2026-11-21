from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from llm.client import LLMClient
from tools import cluster_config
from tools import topology as topology_tools
from tools.schemas import build_tools, REPAIR_TOOLS, DIAGNOSIS_TOOL, ROOT_CAUSE_LABELS
from storage.sqlite_store import DESTRUCTIVE_TOOLS
from tools import k8s, node_etcd as netcd
from tools.repairs import (
    restart_member, member_remove_readd, kube_etcd_switch_rebuild,
    snapshot_restore_full, reset_cluster,
)
from tools.truncate import truncate


def _check_node(value: str, default: str | None = None) -> tuple[str | None, str]:
    nodes = cluster_config.nodes()
    node = value or default or cluster_config.default_node()
    if node not in nodes:
        return None, f"invalid node {value!r}; expected one of {', '.join(nodes)}"
    return node, ""


def _exec_diagnostic(name: str, args: dict) -> str:
    if name == "kubectl_get":
        return k8s.kubectl_get(args.get("resource", ""), args.get("namespace", ""))
    if name == "kubectl_describe":
        return k8s.kubectl_describe(args.get("resource", ""), args.get("name", ""), args.get("namespace", ""))
    if name == "kubectl_logs":
        return k8s.kubectl_logs(args.get("pod", ""), args.get("container", ""), args.get("tail_lines", 100), args.get("namespace", "default"))
    if name == "kubectl_events":
        return k8s.kubectl_events(args.get("namespace", "default"))
    if name == "etcdctl_endpoint_health":
        return netcd.etcdctl_endpoint_health()
    if name in {"etcdctl_endpoint_status", "etcdctl_member_list", "etcdctl_alarm_list"}:
        node, err = _check_node(args.get("node", ""))
        if err:
            return err
        if name == "etcdctl_endpoint_status":
            return netcd.etcdctl_endpoint_status(node)
        if name == "etcdctl_member_list":
            return netcd.etcdctl_member_list(node)
        return netcd.etcdctl_alarm_list(node)
    if name in {"journalctl_tail", "crictl_ps", "crictl_logs", "systemctl_status", "df_path", "ls_path"}:
        node, err = _check_node(args.get("node", ""))
        if err:
            return err
        if name == "journalctl_tail":
            return netcd.journalctl_tail(node, args.get("unit", ""), args.get("lines", 100))
        if name == "crictl_ps":
            return netcd.crictl_ps(node)
        if name == "crictl_logs":
            return netcd.crictl_logs(node, args.get("container_id", ""), args.get("tail", 100))
        if name == "systemctl_status":
            return netcd.systemctl_status(node, args.get("unit", ""))
        if name == "df_path":
            return netcd.df_path(node, args.get("path", ""))
        return netcd.ls_path(node, args.get("path", ""))
    return f"unknown tool: {name}"


def _exec_repair(name: str, args: dict) -> str:
    node, err = _check_node(args.get("node", ""))
    if err:
        return err
    if name == "restart_member":
        return restart_member(node)
    if name == "member_remove_readd":
        return member_remove_readd(node)
    if name == "kube_etcd_switch_rebuild":
        return kube_etcd_switch_rebuild(node)
    if name == "snapshot_restore_full":
        return snapshot_restore_full()
    if name == "reset_cluster":
        return reset_cluster()
    return f"unknown repair: {name}"


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


def system_prompt(topology: dict | None = None) -> str:
    control_planes = list(topology.get("control_planes") or []) if topology else cluster_config.nodes()
    probe = topology.get("probe_node") if topology else None
    probe = probe or cluster_config.default_node()
    count = len(control_planes)
    if topology and count:
        quorum_line = (
            f"Etcd quorum is {topology['quorum']} of {count}; stopping "
            f"{topology['quorum_loss_targets']} member(s) removes write quorum."
        )
    else:
        quorum_line = "Quorum depends on the live etcd member count; confirm it with etcdctl_member_list."
    return f"""You are an etcd fault diagnosis and repair agent for a {count}-control-plane Kubernetes cluster.

The discoverable node names are: {', '.join(control_planes)}. Use only those exact names for node arguments.
The default probe node is {probe}.
Read-only investigation tools are safe to call at any time. Submit a diagnosis with submit_diagnosis before any repair.
root_cause MUST be exactly one of: {', '.join(ROOT_CAUSE_LABELS)}. Do not write a sentence, a node name, or an explanation in root_cause.

If the diagnosis is accepted, submit_diagnosis returns an approval_token. Every repair call MUST include that exact approval_token.
If the diagnosis is rejected, do not repair and do not brute-force labels; gather one new decisive observation before submitting again.
Use etcdctl_member_list and etcdctl_endpoint_health from the default probe node, plus pod status and logs, to identify the affected member.
Investigate briefly: use at most four assistant turns of read-only tools, then call submit_diagnosis immediately. Do not keep investigating once the likely root cause is clear.
A stopped or unreachable member is etcd_member_unreachable. A wiped data directory is etcd_member_data_loss.
A wrong peer URL is etcd_config_error. A full disk is etcd_disk_exhaustion. {quorum_line}
A healthy cluster with only noisy events is no_fault. Do not repair a healthy cluster.
Choose only repairs allowed by the scenario. Keep steps efficient."""


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

    state = store.get_state(run_id) if (store is not None and resume) else {}
    messages: list[dict[str, Any]] = store.load_messages(run_id) if (store is not None and resume) else []
    if not messages:
        messages = [
            {"role": "system", "content": system_prompt(topology_tools.frozen_topology())},
            {"role": "user", "content": "Investigate the Kubernetes cluster for an etcd fault. Diagnose and repair if needed."},
        ]
        if store is not None:
            for message in messages:
                store.append_message(run_id, message["role"], message["content"], backend=getattr(backend, "name", "harness"))

    diagnosis = state.get("diagnosis")
    diagnosis_raw = state.get("diagnosis_raw")
    diagnosis_submitted = bool(state.get("diagnosis_submitted", diagnosis is not None))
    approval_token = state.get("approval_token")
    diagnosis_correct = bool(state.get("diagnosis_correct", False))
    approval_source = state.get("approval_source")
    repair = state.get("repair")
    repair_args = state.get("repair_args")
    policy_violations: list[str] = list(state.get("policy_violations") or [])
    forbidden_reason = state.get("forbidden_reason")
    steps = int(state.get("steps", 0))
    tokens_used = int(state.get("tokens_used", 0))
    diagnosis_nudged = bool(state.get("diagnosis_nudged", False))
    max_steps = scenario.get("max_steps", 40)
    max_tokens = scenario.get("max_tokens", 60000)
    diagnosis_deadline = scenario.get("diagnosis_deadline_steps", 4)
    acceptable = set(scenario.get("acceptable_repairs", []))
    forbidden = set(scenario.get("forbidden_repairs", []))
    llm_error = False
    end_reason = "submitted" if diagnosis_submitted else "agent_stopped"

    if store is not None:
        store.update_run(run_id, status="running", phase="agent_running", end_reason=None)

    def checkpoint() -> dict[str, Any]:
        snapshot = {
            "diagnosis": diagnosis,
            "diagnosis_raw": diagnosis_raw,
            "diagnosis_submitted": diagnosis_submitted,
            "approval_token": approval_token,
            "diagnosis_correct": diagnosis_correct,
            "approval_source": approval_source,
            "repair": repair,
            "repair_args": repair_args,
            "policy_violations": policy_violations,
            "forbidden_reason": forbidden_reason,
            "steps": steps,
            "tokens_used": tokens_used,
            "diagnosis_nudged": diagnosis_nudged,
            "trace_path": str(trace_path),
            "seq": int(store.get_run(run_id)["seq"]) if store is not None else 0,
        }
        if store is not None:
            store.set_state(run_id, snapshot)
            store.update_run(
                run_id,
                diagnosis=diagnosis,
                diagnosis_raw=diagnosis_raw,
                diagnosis_correct=(diagnosis == scenario["root_cause"]) if diagnosis is not None else None,
                approval_granted=approval_token is not None,
                repair=repair,
                repair_args_json=repair_args,
                policy_violations_json=policy_violations,
                forbidden_reason=forbidden_reason,
                steps=steps,
                tokens_used=tokens_used,
                trace_path=str(trace_path),
            )
        return snapshot

    log({"type": "resume_started", "from_seq": state.get("seq", 0)} if resume else {"type": "setup", "reset_ok": reset_ok, "baseline_ok": baseline_ok, "inject_ok": inject_ok, "output": truncate(setup_output, 2000)})
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
                tools=build_tools(),
                temperature=0.2,
                max_tokens=2048,
                on_chunk=(lambda text: emit("assistant_delta", text)) if reporter is not None else None,
            )
        except Exception as e:
            llm_error = True
            end_reason = "error"
            if store is not None:
                store.add_llm_request(run_id, getattr(backend, "name", "unknown"), int((time.time() - started) * 1000), 0, 0, str(e))
            log({"type": "llm_error", "error": str(e)})
            artifact("llm_error_full", {"error": str(e)})
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
        if not tool_calls and not diagnosis_submitted:
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
            end_reason = "submitted" if diagnosis_submitted else "agent_stopped"
            break

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
            result = ""
            call_status = "finished"
            approved_by = None
            call_error = None
            call_id = f"{tc.get('id', 'call')}-{steps}-{len(messages)}"
            mutating = name in REPAIR_TOOLS
            destructive = name in DESTRUCTIVE_TOOLS
            if store is not None:
                store.begin_tool_call(run_id, call_id, len(messages) - 1, name, log_args, mutating, destructive)

            if name == DIAGNOSIS_TOOL:
                diagnosis_raw = args.get("root_cause")
                diagnosis = normalize_diagnosis(diagnosis_raw)
                diagnosis_submitted = True
                diagnosis_correct = diagnosis == scenario["root_cause"]
                approved = diagnosis_correct
                if approved and approve == "prompt":
                    if pause_reporter is not None:
                        pause_reporter()
                    try:
                        try:
                            answer = input(f"Approve repair for {diagnosis}? [y/N] ").strip().lower()
                        except EOFError:
                            answer = ""
                    finally:
                        if resume_reporter is not None:
                            resume_reporter()
                    approved = answer in {"y", "yes"}
                    approval_source = "operator"
                elif approved:
                    approval_source = "policy"
                approval_token = f"approved:{run_id}" if approved else None
                if not diagnosis_correct:
                    result = "diagnosis rejected; gather new evidence before resubmitting; do not brute-force labels"
                elif approve == "prompt" and not approved:
                    result = "diagnosis correct; operator declined repair approval"
                elif approve == "prompt":
                    result = "diagnosis accepted; approval_token suppressed (prompt mode)"
                else:
                    result = f"diagnosis accepted; approval_token={approval_token}"
                approved_by = approval_source if approved else None
                log({
                    "type": "approval",
                    "granted": approved,
                    "diagnosis": diagnosis,
                    "diagnosis_correct": diagnosis_correct,
                    "mode": approve,
                    "raw": diagnosis_raw,
                })
                if store is not None:
                    store.add_diagnosis_attempt(run_id, diagnosis_raw, diagnosis, diagnosis_correct)
                    store.update_tool_call_status(run_id, call_id, "started")
            elif name in REPAIR_TOOLS:
                safe_args = {k: v for k, v in args.items() if k != "approval_token"}
                reason = None
                if not diagnosis_submitted:
                    reason = "BLOCKED: diagnosis required first"
                elif approval_token is None:
                    reason = (
                        "BLOCKED: approval not granted"
                        if diagnosis_correct
                        else "BLOCKED: diagnosis rejected; no approval token"
                    )
                elif args.get("approval_token") != approval_token:
                    reason = "BLOCKED: missing or invalid approval_token"
                elif name in forbidden:
                    reason = "BLOCKED: forbidden repair for this scenario"
                elif name not in acceptable:
                    reason = "BLOCKED: repair not acceptable for this scenario"
                if reason:
                    result = reason
                    call_status = "denied"
                    call_error = reason
                    policy_violations.append(f"{name}: {reason}")
                    if "forbidden" in reason:
                        forbidden_reason = reason
                    if store is not None:
                        store.add_policy_violation(run_id, name, reason)
                else:
                    approved_by = approval_source or ("operator" if approve == "prompt" else "policy")
                    if store is not None:
                        store.update_tool_call_status(run_id, call_id, "started")
                    checkpoint()
                    try:
                        result = _exec_repair(name, args)
                        repair = name
                        repair_args = safe_args
                    except Exception as e:
                        result = f"ERROR: {e}"
                    call_status = "finished" if not result.startswith("ERROR:") else "failed"
                    if store is not None:
                        artifact("repair_output_full", {"tool": name, "args": safe_args, "result": result})
            else:
                if store is not None:
                    store.update_tool_call_status(run_id, call_id, "started")
                try:
                    result = _exec_diagnostic(name, args)
                except Exception as e:
                    result = f"ERROR: {e}"
                call_status = "finished" if not result.startswith("ERROR:") else "failed"
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

        if not diagnosis_submitted and not diagnosis_nudged and steps >= diagnosis_deadline:
            nudge = {"role": "user", "content": "Stop investigating now. Call submit_diagnosis with your best exact root_cause label in the next response."}
            messages.append(nudge)
            if store is not None:
                store.append_message(run_id, "user", nudge["content"], backend="harness")
            diagnosis_nudged = True
            log({"type": "nudge", "step": steps})
            checkpoint()

    if steps >= max_steps:
        end_reason = "budget_steps"
    elif tokens_used >= max_tokens:
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
        "diagnosis": diagnosis,
        "diagnosis_raw": diagnosis_raw,
        "truth": scenario["root_cause"],
        "diagnosis_correct": diagnosis == scenario["root_cause"],
        "approval_granted": approval_token is not None,
        "repair": repair,
        "repair_args": repair_args,
        "policy_violations": policy_violations,
        "forbidden_reason": forbidden_reason,
        "verify_ok": verify_ok,
        "verify_output": truncate(verify_output, 1000),
        "steps": steps,
        "tokens_used": tokens_used,
        "trace": str(trace_path),
    }
    log({"type": "record", "record": record})
    final_state = checkpoint()
    loop_success = bool(
        record["diagnosis_correct"]
        and verify_ok
        and (bool(repair) != (scenario["root_cause"] == "no_fault"))
    )
    score = {
        "scorer_version": "legacy-record-v1",
        "diagnosis": diagnosis,
        "diagnosis_correct": record["diagnosis_correct"],
        "repair_action": repair,
        "repair_acceptable": bool(repair and repair in acceptable),
        "repair_forbidden": bool(repair and repair in forbidden),
        "recovered": verify_ok,
        "policy_violations": len(policy_violations),
        "loop_success": loop_success,
    }
    if store is not None:
        store.finish_run(run_id, record, status="finished", end_reason=end_reason, state=final_state, score=score)
    return record
