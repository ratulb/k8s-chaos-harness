from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from llm.client import LLMClient
from tools.schemas import TOOLS, REPAIR_TOOLS, DIAGNOSIS_TOOL
from tools import k8s, node_etcd as netcd
from tools.repairs import (
    restart_member, member_remove_readd, kube_etcd_switch_rebuild,
    snapshot_restore_full, reset_cluster,
)
from tools.truncate import truncate


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
    if name == "etcdctl_endpoint_status":
        return netcd.etcdctl_endpoint_status(args.get("node", "vm1"))
    if name == "etcdctl_member_list":
        return netcd.etcdctl_member_list(args.get("node", "vm1"))
    if name == "etcdctl_alarm_list":
        return netcd.etcdctl_alarm_list(args.get("node", "vm1"))
    if name == "journalctl_tail":
        return netcd.journalctl_tail(args.get("node", ""), args.get("unit", ""), args.get("lines", 100))
    if name == "crictl_ps":
        return netcd.crictl_ps(args.get("node", ""))
    if name == "crictl_logs":
        return netcd.crictl_logs(args.get("node", ""), args.get("container_id", ""), args.get("tail", 100))
    if name == "systemctl_status":
        return netcd.systemctl_status(args.get("node", ""), args.get("unit", ""))
    if name == "df_path":
        return netcd.df_path(args.get("node", ""), args.get("path", ""))
    if name == "ls_path":
        return netcd.ls_path(args.get("node", ""), args.get("path", ""))
    return f"unknown tool: {name}"


def _exec_repair(name: str, args: dict) -> str:
    node = args.get("node", "")
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


SYSTEM_PROMPT = """You are an etcd fault diagnosis and repair agent for a Kubernetes cluster.
Investigate with read-only tools. You MUST call submit_diagnosis exactly once before finishing or repairing.
Check pod status and etcd member list. A stopped member appears as missing or NotReady. Use root cause label no_fault if the cluster is healthy. Only after diagnosis may you call repair tools.
Choose repairs from acceptable actions; forbidden repairs are blocked. Keep steps efficient."""


def run_scenario(scenario: dict, backend, run_id: str, inject_cmd: str, verify_cmd: str, trace_dir: str = "traces") -> dict:
    from llm.client import LLMClient

    Path(trace_dir).mkdir(parents=True, exist_ok=True)
    trace_path = Path(trace_dir) / f"{run_id}.jsonl"
    trace = []

    def log(event: dict) -> None:
        event["ts"] = time.time()
        trace.append(event)
        with trace_path.open("a") as f:
            f.write(json.dumps(event) + "\n")

    # inject
    log({"type": "inject", "cmd": inject_cmd})
    subprocess.run(inject_cmd, shell=True, check=False)

    client = LLMClient(backend)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "Investigate the Kubernetes cluster for an etcd fault. Diagnose and repair if needed."},
    ]
    diagnosis = None
    diagnosis_submitted = False
    steps = 0
    tokens_used = 0
    max_steps = scenario.get("max_steps", 40)
    max_tokens = scenario.get("max_tokens", 60000)
    acceptable = set(scenario.get("acceptable_repairs", []))
    forbidden = set(scenario.get("forbidden_repairs", []))

    while steps < max_steps and tokens_used < max_tokens:
        steps += 1
        try:
            resp = client.chat(messages, tools=TOOLS, temperature=0.2, max_tokens=2048)
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
                messages.append({"role": "user", "content": "You have not submitted a diagnosis. Investigate more if needed, then call submit_diagnosis with the root cause."})
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
                diagnosis = args.get("root_cause")
                diagnosis_submitted = True
                result = "diagnosis submitted"
            elif name in REPAIR_TOOLS:
                if not diagnosis_submitted:
                    result = "BLOCKED: diagnosis required first"
                elif name in forbidden:
                    result = "BLOCKED: forbidden repair for this scenario"
                elif name not in acceptable:
                    result = "BLOCKED: repair not acceptable for this scenario"
                else:
                    try:
                        result = _exec_repair(name, args)
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

    # verify
    try:
        v = subprocess.run(verify_cmd, shell=True, capture_output=True, text=True, timeout=300)
        verify_ok = (v.returncode == 0)
        verify_output = (v.stdout + v.stderr).strip()
    except Exception as e:
        verify_ok = False
        verify_output = str(e)
    log({"type": "verify", "ok": verify_ok, "output": verify_output})

    record = {
        "run_id": run_id,
        "scenario": scenario["id"],
        "backend": backend.name,
        "diagnosis": diagnosis,
        "truth": scenario["root_cause"],
        "diagnosis_correct": diagnosis == scenario["root_cause"],
        "verify_ok": verify_ok,
        "steps": steps,
        "tokens_used": tokens_used,
        "trace": str(trace_path),
    }
    log({"type": "record", "record": record})
    return record
