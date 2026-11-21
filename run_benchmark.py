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
from tools import cluster_config
from harness.agent import run_scenario
from storage.sqlite_store import SQLiteStore

BACKENDS = {
    "kaggle": lambda: Backend(name="kaggle", base_url=os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434"), model=os.environ.get("OLLAMA_MODEL", "qwen3.8:27b"), api_key=os.environ.get("OLLAMA_API_KEY", "ollama")),
    "groq": lambda: Backend(name="groq", base_url="https://api.groq.com/openai/v1", model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"), api_key=os.environ["GROQ_API_KEY"]),
    "openrouter": lambda: Backend(name="openrouter", base_url="https://openrouter.ai/api/v1", model=os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"), api_key=os.environ["OPENROUTER_API_KEY"]),
    "cloudflare": lambda: Backend(name="cloudflare", base_url=f"https://api.cloudflare.com/client/v4/accounts/{os.environ['CLOUDFLARE_ACCOUNT_ID']}/ai/v1", model=os.environ.get("CLOUDFLARE_MODEL", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"), api_key=os.environ["CLOUDFLARE_AUTH_TOKEN"]),
}


def _run(cmd: str, timeout: int, env: dict[str, str] | None = None) -> tuple[bool, str]:
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode == 0, (p.stdout + p.stderr).strip()
    except Exception as e:
        return False, str(e)


def _load_scenario(root: str, scenario_id: str) -> dict:
    return json.load(open(os.path.join(root, "scenarios", scenario_id + ".json")))


def _artifact(store: SQLiteStore, run_id: str, kind: str, output: str) -> None:
    store.add_artifact(run_id, kind, {"output": output})


def _terminal_record(
    run_id: str,
    scenario: dict,
    backend,
    reset_ok: bool,
    baseline_ok: bool,
    setup_output: str,
    reason: str,
) -> dict:
    from tools.truncate import truncate
    return {
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
        "end_reason": reason,
    }


def _run_fresh(
    store: SQLiteStore,
    run_row: dict,
    scenario: dict,
    backend,
    inject_cmd: str,
    verify_cmd: str,
    reset_cmd: str,
    run_env: dict[str, str],
    timeout: int,
    trace_dir: str,
    runs_path: str | None,
) -> dict:
    run_id = run_row["run_id"]
    owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    if not store.start_lease(run_id, owner):
        raise RuntimeError(f"run {run_id} is already leased")

    setup_parts: list[str] = []
    reset_ok = True
    baseline_ok = True

    store.update_run(run_id, status="running", phase="resetting")
    if reset_cmd:
        reset_ok, reset_out = _run(reset_cmd, 300, run_env)
        setup_parts.append(f"RESET: {reset_out}")
        _artifact(store, run_id, "reset_output_full", reset_out)
        store.add_event(run_id, "reset", {"ok": reset_ok, "output": reset_out[:2000]})
        store.update_run(run_id, reset_ok=reset_ok)

    if reset_ok:
        store.update_run(run_id, phase="baseline")
        baseline_ok, base_out = _run(verify_cmd, timeout, run_env)
        setup_parts.append(f"BASELINE: {base_out}")
        _artifact(store, run_id, "baseline_output_full", base_out)
        store.add_event(run_id, "baseline", {"ok": baseline_ok, "output": base_out[:2000]})
        store.update_run(run_id, baseline_ok=baseline_ok)

    setup_output = "\n".join(setup_parts)
    if not reset_ok or not baseline_ok:
        reason = "reset_failed" if not reset_ok else "baseline_failed"
        rec = _terminal_record(run_id, scenario, backend, reset_ok, baseline_ok, setup_output, reason)
        store.finish_run(run_id, rec, status="aborted", end_reason=reason)
        if runs_path:
            store.export_all(runs_path, trace_dir)
        return rec

    store.update_run(run_id, phase="injecting")
    inject_ok, inject_out = _run(inject_cmd, 120, run_env)
    setup_parts.append(f"INJECT: {inject_out}")
    setup_output = "\n".join(setup_parts)
    _artifact(store, run_id, "injection_output_full", inject_out)
    store.add_event(run_id, "inject", {"ok": inject_ok, "output": inject_out[:2000]})
    store.update_run(run_id, inject_ok=inject_ok)

    rec = run_scenario(
        scenario,
        backend,
        run_id,
        inject_cmd,
        verify_cmd,
        trace_dir=trace_dir,
        verify_timeout=timeout,
        baseline_ok=baseline_ok,
        reset_ok=reset_ok,
        inject_ok=inject_ok,
        setup_output=setup_output,
        store=store,
        resume=False,
    )
    if runs_path:
        store.export_all(runs_path, trace_dir)
    return rec


def _run_resume(
    store: SQLiteStore,
    row: dict,
    scenario: dict,
    backend,
    root: str,
    trace_dir: str,
    runs_path: str | None,
) -> dict:
    run_id = row["run_id"]
    owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    if not store.start_lease(run_id, owner):
        raise RuntimeError(f"run {run_id} is already leased")

    for call in store.unfinished_tool_calls(run_id):
        if not call["mutating"]:
            store.finish_tool_call(
                run_id,
                call["call_id"],
                "finished",
                "superseded_after_resume",
                "superseded_after_resume",
                False,
                error="read-only call superseded by resume",
            )

    verify_cmd = scenario["verify"]
    if not os.path.isabs(verify_cmd):
        verify_cmd = os.path.join(root, verify_cmd)
    rec = run_scenario(
        scenario,
        backend,
        run_id,
        "",
        verify_cmd,
        trace_dir=trace_dir,
        verify_timeout=300,
        store=store,
        resume=True,
    )
    if runs_path:
        store.export_all(runs_path, trace_dir)
    return rec


def _abort_and_retry(
    store: SQLiteStore,
    row: dict,
    reason: str,
    config_hash: str,
    spec_hash: str,
    scenario_version: int,
) -> dict:
    old_id = row["run_id"]
    store.add_event(old_id, "resume_aborted", {"reason": reason})
    store.finish_run(old_id, status="aborted", end_reason=reason)
    attempt = int(row.get("resume_count", 0)) + 1
    new_id = f"{old_id}-retry{attempt}"
    if store.run_exists(new_id):
        new_id = f"{new_id}-{uuid.uuid4().hex[:6]}"
    return store.create_run(
        new_id,
        batch_id=row.get("batch_id"),
        attempt=attempt,
        resume_of=old_id,
        resume_count=attempt,
        scenario_id=row["scenario_id"],
        scenario_version=scenario_version,
        spec_hash=spec_hash,
        backend=row["backend"],
        model=row.get("model"),
        temperature=row.get("temperature", 0.2),
        seed=row.get("seed", 0),
        status="pending",
        phase="queued",
        truth=row.get("truth"),
        config_hash=config_hash,
        harness_git_sha=store.git_sha(),
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--backend", required=True, choices=list(BACKENDS))
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--reset-cmd", default="cluster/restore_clean_state.sh")
    ap.add_argument("--db", default=os.environ.get("CHECKPOINT_DB", "results.sqlite3"))
    ap.add_argument("--batch-id", default=None)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--resume-of", default=None)
    ap.add_argument("--force-resume", action="store_true")
    ap.add_argument("--no-jsonl", action="store_true")
    ap.add_argument("--trace-dir", default="traces")
    args = ap.parse_args()

    root = os.path.dirname(os.path.abspath(__file__))
    trace_dir = args.trace_dir if os.path.isabs(args.trace_dir) else os.path.join(root, args.trace_dir)
    runs_path = None if args.no_jsonl else os.path.join(root, "runs.jsonl")

    store = SQLiteStore(args.db)
    scenario = _load_scenario(root, args.scenario)
    spec_hash = store.hash_spec(scenario)
    spec_json = json.dumps(scenario, sort_keys=True)
    scenario_version = store.ensure_scenario(scenario["id"], spec_hash, spec_json)

    backend = BACKENDS[args.backend]()
    if not LLMClient(backend).health():
        print(f"Backend {backend.name} not healthy", file=sys.stderr)
        sys.exit(1)

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

    run_env = os.environ.copy()
    targets = scenario.get("target_nodes", ["second"])
    if isinstance(targets, str):
        targets = [targets]
    resolved = [cluster_config.node_by_role(role) for role in targets]
    run_env["TARGET_NODE"] = resolved[0]
    run_env["TARGET_NODES"] = " ".join(resolved)

    config_hash = store.hash_spec(
        {
            "targets": resolved,
            "reset_cmd": reset_cmd,
            "backend": backend.name,
            "model": backend.model,
            "temperature": 0.2,
        }
    )
    batch_id = store.create_batch(
        batch_id=args.batch_id,
        command=sys.argv,
        scenario_ids=[scenario["id"]],
        backend=backend.name,
        seed_start=args.seed,
        runs_requested=args.runs,
        reset_cmd=reset_cmd,
        config_hash=config_hash,
    )

    if args.resume or args.resume_of:
        if args.resume_of:
            rows = [store.get_run(args.resume_of)]
        else:
            rows = store.resume_candidates(args.scenario, args.backend)[: max(1, args.runs)]
        if not rows:
            print("No resumable runs found", file=sys.stderr)
            sys.exit(1)

        for row in rows:
            if row["scenario_id"] != scenario["id"] or row["backend"] != backend.name:
                print(f"run {row['run_id']} does not match --scenario/--backend", file=sys.stderr)
                continue
            reason = None
            if row.get("phase") in {"queued", "resetting", "baseline", "injecting"}:
                reason = "unsafe_resume_phase"
            elif row.get("config_hash") and row["config_hash"] != config_hash and not args.force_resume:
                reason = "resume_config_mismatch"
            elif store.has_unknown_mutating_tool_call(row["run_id"]):
                reason = "unknown_repair_side_effect"

            if reason:
                new_row = _abort_and_retry(store, row, reason, config_hash, spec_hash, scenario_version)
                print(f"=== aborted {row['run_id']} ({reason}); retry {new_row['run_id']} ===", flush=True)
                rec = _run_fresh(
                    store, new_row, scenario, backend, inject_cmd, verify_cmd,
                    reset_cmd, run_env, timeout, trace_dir, runs_path,
                )
            else:
                print(f"=== resume {row['run_id']} ===", flush=True)
                rec = _run_resume(store, row, scenario, backend, root, trace_dir, runs_path)
            print(json.dumps(rec), flush=True)
        store.update_batch(batch_id, "complete")
        return

    for i in range(args.runs):
        run_id = f"{scenario['id']}-{backend.name}-{args.seed + i}-{uuid.uuid4().hex[:8]}"
        print(f"=== run {i+1}/{args.runs}: {run_id} ===", flush=True)
        run_row = store.create_run(
            run_id,
            batch_id=batch_id,
            scenario_id=scenario["id"],
            scenario_version=scenario_version,
            spec_hash=spec_hash,
            backend=backend.name,
            model=backend.model,
            temperature=0.2,
            seed=args.seed + i,
            status="pending",
            phase="queued",
            truth=scenario["root_cause"],
            config_hash=config_hash,
            harness_git_sha=store.git_sha(),
        )
        rec = _run_fresh(
            store, run_row, scenario, backend, inject_cmd, verify_cmd,
            reset_cmd, run_env, timeout, trace_dir, runs_path,
        )
        print(json.dumps(rec), flush=True)

    store.update_batch(batch_id, "complete")


if __name__ == "__main__":
    main()
