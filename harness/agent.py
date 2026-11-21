from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from llm.client import LLMClient
from tools import cluster_config
from tools.schemas import build_tools, REPAIR_TOOLS, DIAGNOSIS_TOOL, ROOT_CAUSE_LABELS
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


def system_prompt() -> str:
    return f"""You are an etcd fault diagnosis and repair agent for a 3-control-plane Kubernetes cluster.

The discoverable node names are: {', '.join(cluster_config.nodes())}. Use only those exact names for node arguments.
Read-only investigation tools are safe to call at any time. Submit a diagnosis with submit_diagnosis before any repair.
root_cause MUST be exactly one of: {', '.join(ROOT_CAUSE_LABELS)}. Do not write a sentence, a node name, or an explanation in root_cause.

If the diagnosis is accepted, submit_diagnosis returns an approval_token. Every repair call MUST include that exact approval_token.
If the diagnosis is rejected, do not repair and do not brute-force labels; gather one new decisive observation before submitting again.
Use etcdctl_member_list and etcdctl_endpoint_health from the default probe node, plus pod status and logs, to identify the affected member.
Investigate briefly: use at most four assistant turns of read-only tools, then call submit_diagnosis immediately. Do not keep investigating once the likely root cause is clear.
A stopped or unreachable member is etcd_member_unreachable. A wiped data directory is etcd_member_data_loss.
A wrong peer URL is etcd_config_error. A full disk is etcd_disk_exhaustion. Two stopped members are etcd_quorum_loss.
A healthy cluster with only noisy events is no_fault. Do not repair a healthy cluster.
Choose only repairs allowed by the scenario. Keep steps efficient."""


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
) -> dict:
    Path(trace_dir).mkdir(parents=True, exist_ok=True)
    trace_path = Path(trace_dir) / f"{run_id}.jsonl"
    trace = []

    def log(event: dict) -> None:
        event["ts"] = time.time()
        trace.append(event)
        with trace_path.open("a") as f:
            f.write(json.dumps(event) + "\n")

    log({"type": "setup", "reset_ok": reset_ok, "baseline_ok": baseline_ok, "inject_ok": inject_ok, "output": truncate(setup_output, 2000)})

    client = LLMClient(backend)
    messages = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": "Investigate the Kubernetes cluster for an etcd fault. Diagnose and repair if needed."},
    ]
    diagnosis = None
    diagnosis_raw = None
    diagnosis_submitted = False
    approval_token = None
    repair = None
    repair_args = None
    policy_violations: list[str] = []
    forbidden_reason = None
    steps = 0
    tokens_used = 0
    max_steps = scenario.get("max_steps", 40)
    max_tokens = scenario.get("max_tokens", 60000)
    diagnosis_deadline = scenario.get("diagnosis_deadline_steps", 4)
    diagnosis_nudged = False
    acceptable = set(scenario.get("acceptable_repairs", []))
    forbidden = set(scenario.get("forbidden_repairs", []))

    while steps < max_steps and tokens_used < max_tokens:
        steps += 1
        try:
            resp = client.chat(messages, tools=build_tools(), temperature=0.2, max_tokens=2048)
        except Exception as e:
            log({"type": "llm_error", "error": str(e)})
            break
        usage = resp.get("usage", {}) or {}
        tokens_used += usage.get("total_tokens", 0) or 0
        msg = resp["choices"][0]["message"]
        messages.append(msg)
        log({"type": "assistant", "message": msg})

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls and not diagnosis_submitted:
            if steps < max_steps:
                messages.append({"role": "user", "content": "You have not submitted a diagnosis. Call submit_diagnosis with one exact root cause label."})
                continue
            break
        if not tool_calls:
            break

        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                args = {}
            result = ""
            if name == DIAGNOSIS_TOOL:
                diagnosis_raw = args.get("root_cause")
                diagnosis = normalize_diagnosis(diagnosis_raw)
                diagnosis_submitted = True
                if diagnosis == scenario["root_cause"]:
                    approval_token = f"approved:{run_id}"
                    result = f"diagnosis accepted; approval_token={approval_token}"
                else:
                    approval_token = None
                    result = "diagnosis rejected; gather new evidence before resubmitting; do not brute-force labels"
                log({"type": "approval", "granted": approval_token is not None, "diagnosis": diagnosis, "raw": diagnosis_raw})
            elif name in REPAIR_TOOLS:
                reason = None
                if not diagnosis_submitted:
                    reason = "BLOCKED: diagnosis required first"
                elif approval_token is None:
                    reason = "BLOCKED: diagnosis rejected; no approval token"
                elif args.get("approval_token") != approval_token:
                    reason = "BLOCKED: missing or invalid approval_token"
                elif name in forbidden:
                    reason = "BLOCKED: forbidden repair for this scenario"
                elif name not in acceptable:
                    reason = "BLOCKED: repair not acceptable for this scenario"
                if reason:
                    result = reason
                    policy_violations.append(f"{name}: {reason}")
                    if "forbidden" in reason:
                        forbidden_reason = reason
                else:
                    try:
                        result = _exec_repair(name, args)
                        repair = name
                        repair_args = {k: v for k, v in args.items() if k != "approval_token"}
                    except Exception as e:
                        result = f"ERROR: {e}"
            else:
                try:
                    result = _exec_diagnostic(name, args)
                except Exception as e:
                    result = f"ERROR: {e}"
            result = truncate(result, 4000)
            tool_msg = {"role": "tool", "tool_call_id": tc["id"], "content": result}
            messages.append(tool_msg)
            log({"type": "tool", "name": name, "args": args, "result": result})

        if not diagnosis_submitted and not diagnosis_nudged and steps >= diagnosis_deadline:
            messages.append({"role": "user", "content": "Stop investigating now. Call submit_diagnosis with your best exact root_cause label in the next response."})
            diagnosis_nudged = True
            log({"type": "nudge", "step": steps})

    try:
        v = subprocess.run(verify_cmd, shell=True, capture_output=True, text=True, timeout=verify_timeout)
        verify_ok = (v.returncode == 0)
        verify_output = (v.stdout + v.stderr).strip()
    except Exception as e:
        verify_ok = False
        verify_output = str(e)
    log({"type": "verify", "ok": verify_ok, "output": truncate(verify_output, 2000)})

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
    return record
