#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm.env import load_env
load_env()
from llm.client import Backend, LLMClient
from harness.agent import run_scenario
from tools.truncate import truncate

BACKENDS = {
    "kaggle": lambda: Backend(name="kaggle", base_url=os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434"), model=os.environ.get("OLLAMA_MODEL", "qwen3.8:27b"), api_key=os.environ.get("OLLAMA_API_KEY", "ollama")),
    "groq": lambda: Backend(name="groq", base_url="https://api.groq.com/openai/v1", model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"), api_key=os.environ["GROQ_API_KEY"]),
    "openrouter": lambda: Backend(name="openrouter", base_url="https://openrouter.ai/api/v1", model=os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"), api_key=os.environ["OPENROUTER_API_KEY"]),
    "cloudflare": lambda: Backend(name="cloudflare", base_url=f"https://api.cloudflare.com/client/v4/accounts/{os.environ['CLOUDFLARE_ACCOUNT_ID']}/ai/v1", model=os.environ.get("CLOUDFLARE_MODEL", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"), api_key=os.environ["CLOUDFLARE_AUTH_TOKEN"]),
}


def _run(cmd: str, timeout: int) -> tuple[bool, str]:
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, (p.stdout + p.stderr).strip()
    except Exception as e:
        return False, str(e)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--backend", required=True, choices=list(BACKENDS))
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--reset-cmd", default="cluster/restore_clean_state.sh")
    args = ap.parse_args()

    root = os.path.dirname(os.path.abspath(__file__))
    scenario = json.load(open(os.path.join(root, "scenarios", args.scenario + ".json")))
    inject_cmd = scenario["inject"]
    if not os.path.isabs(inject_cmd):
        inject_cmd = os.path.join(root, inject_cmd)
    verify_cmd = scenario["verify"]
    if not os.path.isabs(verify_cmd):
        verify_cmd = os.path.join(root, verify_cmd)
    reset_cmd = args.reset_cmd
    if reset_cmd and not os.path.isabs(reset_cmd):
        reset_cmd = os.path.join(root, reset_cmd)
    timeout = int(scenario.get("timeout_seconds", 300))

    backend = BACKENDS[args.backend]()
    if not LLMClient(backend).health():
        print(f"Backend {backend.name} not healthy", file=sys.stderr)
        sys.exit(1)

    out = open(os.path.join(root, "runs.jsonl"), "a")
    for i in range(args.runs):
        run_id = f"{scenario['id']}-{backend.name}-{args.seed + i}-{uuid.uuid4().hex[:8]}"
        print(f"=== run {i+1}/{args.runs}: {run_id} ===", flush=True)

        setup_output = ""
        reset_ok = True
        baseline_ok = True
        if reset_cmd:
            reset_ok, reset_out = _run(reset_cmd, 300)
            setup_output += f"RESET: {reset_out}\n"
            if not reset_ok:
                baseline_ok = False
        if reset_ok:
            baseline_ok, base_out = _run(verify_cmd, timeout)
            setup_output += f"BASELINE: {base_out}\n"

        if not reset_ok or not baseline_ok:
            rec = {
                "run_id": run_id,
                "scenario": scenario["id"],
                "backend": backend.name,
                "reset_ok": reset_ok,
                "baseline_ok": baseline_ok,
                "inject_ok": False,
                "diagnosis": None,
                "diagnosis_raw": None,
                "truth": scenario["root_cause"],
                "diagnosis_correct": False,
                "approval_granted": False,
                "repair": None,
                "repair_args": None,
                "policy_violations": [],
                "forbidden_reason": None,
                "verify_ok": False,
                "verify_output": truncate(setup_output, 1000),
                "steps": 0,
                "tokens_used": 0,
                "trace": "",
            }
            out.write(json.dumps(rec) + "\n")
            out.flush()
            print(json.dumps(rec), flush=True)
            continue

        inject_ok, inject_out = _run(inject_cmd, 120)
        setup_output += f"INJECT: {inject_out}\n"
        rec = run_scenario(
            scenario,
            backend,
            run_id,
            inject_cmd,
            verify_cmd,
            verify_timeout=timeout,
            baseline_ok=baseline_ok,
            reset_ok=reset_ok,
            inject_ok=inject_ok,
            setup_output=setup_output,
        )
        out.write(json.dumps(rec) + "\n")
        out.flush()
        print(json.dumps(rec), flush=True)
    out.close()


if __name__ == "__main__":
    main()
