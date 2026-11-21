#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm.env import load_env
load_env()
from llm.backends import BACKEND_NAMES, build_backend
from llm.client import LLMClient
from tools import topology as topology_tools
from tools.scenario_schema import load_scenario, validate_scenario
from harness.agent import run_scenario
from harness.reporter import UiReporter
from harness.ui import LiveView
from storage.sqlite_store import active_topology_hash, discard_state, SQLiteStore


def _log_path(logs_dir: str | None, run_id: str) -> str | None:
    if not logs_dir:
        return None
    path = Path(logs_dir) / f"{run_id}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def _append_log(path: str | None, payload: dict) -> None:
    if not path:
        return
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), **payload}, sort_keys=True) + "\n")
    except OSError:
        pass


def _run(
    cmd: str,
    timeout: int,
    env: dict[str, str] | None = None,
    log_path: str | None = None,
) -> tuple[bool, str]:
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, env=env)
        output = (p.stdout + p.stderr).strip()
        ok = p.returncode == 0
    except Exception as e:
        output = str(e)
        ok = False
    _append_log(log_path, {"cmd": cmd, "ok": ok, "output": output})
    return ok, output


def _artifact(store: SQLiteStore, run_id: str, kind: str, output: str) -> None:
    store.add_artifact(run_id, kind, {"output": output})


def _record_topology(store: SQLiteStore, run_id: str, topology_data: dict, capability: dict) -> None:
    store.add_artifact(run_id, "topology_full", json.dumps(topology_data, sort_keys=True), "application/json")
    store.add_artifact(run_id, "capability_full", json.dumps(capability, sort_keys=True), "application/json")
    store.add_event(run_id, "topology", {"topology_hash": topology_data["topology_hash"], "source": topology_data["source"], "confidence": topology_data["confidence"]})
    store.add_event(run_id, "capability", capability)


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
    ui: str = "none",
    logs_dir: str | None = None,
    mode: str = "benchmark",
    approve: str = "policy",
) -> dict:
    run_id = run_row["run_id"]
    log_path = _log_path(logs_dir, run_id)
    owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    if not store.start_lease(run_id, owner):
        raise RuntimeError(f"run {run_id} is already leased")

    setup_parts: list[str] = []
    reset_ok = True
    baseline_ok = True

    store.update_run(run_id, status="running", phase="resetting")
    if reset_cmd:
        reset_ok, reset_out = _run(reset_cmd, 900, run_env, log_path)
        setup_parts.append(f"RESET: {reset_out}")
        _artifact(store, run_id, "reset_output_full", reset_out)
        store.add_event(run_id, "reset", {"ok": reset_ok, "output": reset_out[:2000]})
        store.update_run(run_id, reset_ok=reset_ok)

    if reset_ok:
        store.update_run(run_id, phase="baseline")
        baseline_ok, base_out = _run(verify_cmd, timeout, run_env, log_path)
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
    inject_ok, inject_out = _run(inject_cmd, 120, run_env, log_path)
    setup_parts.append(f"INJECT: {inject_out}")
    setup_output = "\n".join(setup_parts)
    _artifact(store, run_id, "injection_output_full", inject_out)
    store.add_event(run_id, "inject", {"ok": inject_ok, "output": inject_out[:2000]})
    store.update_run(run_id, inject_ok=inject_ok)

    reporter = UiReporter(run_id, store)
    view = LiveView(store.path, run_id, mode=mode) if ui != "none" else None
    if view:
        view.start()
    def pause_view() -> None:
        if view:
            view.stop()
    def resume_view() -> None:
        if view:
            view.start()
    try:
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
            reporter=reporter,
            log_path=log_path,
            approve=approve,
            pause_reporter=pause_view,
            resume_reporter=resume_view,
        )
    finally:
        if view:
            view.stop()
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
    ui: str = "none",
    logs_dir: str | None = None,
    mode: str = "benchmark",
    approve: str = "policy",
) -> dict:
    run_id = row["run_id"]
    log_path = _log_path(logs_dir, run_id)
    if approve == "prompt":
        store.update_run(run_id, interactive=True)
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
    reporter = UiReporter(run_id, store)
    view = LiveView(store.path, run_id, mode=mode) if ui != "none" else None
    if view:
        view.start()
    def pause_view() -> None:
        if view:
            view.stop()
    def resume_view() -> None:
        if view:
            view.start()
    try:
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
            reporter=reporter,
            log_path=log_path,
            approve=approve,
            pause_reporter=pause_view,
            resume_reporter=resume_view,
        )
    finally:
        if view:
            view.stop()
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
    topology_hash: str | None = None,
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
        topology_hash=topology_hash or row.get("topology_hash"),
        interactive=bool(row.get("interactive", False)),
        launch_id=row.get("launch_id"),
        pid=os.getpid(),
        harness_git_sha=store.git_sha(),
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--backend", required=True, choices=list(BACKEND_NAMES))
    ap.add_argument("--mode", choices=["benchmark", "repair"], default="benchmark")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--reset-cmd", default="cluster/restore_clean_state.sh")
    ap.add_argument("--db", default=os.environ.get("CHECKPOINT_DB", "results.sqlite3"))
    ap.add_argument("--batch-id", default=None)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--resume-of", default=None)
    ap.add_argument("--force-resume", action="store_true")
    ap.add_argument("--no-jsonl", action="store_true")
    ap.add_argument("--runs-file", default=None)
    ap.add_argument("--trace-dir", default="traces")
    ap.add_argument("--launch-id", default=None)
    ap.add_argument("--ui", choices=["none", "plain", "live"], default="none")
    ap.add_argument("--logs-dir", default=None)
    ap.add_argument("--approve", choices=["policy", "prompt"], default="policy")
    args = ap.parse_args()

    root = os.path.dirname(os.path.abspath(__file__))
    trace_dir = args.trace_dir if os.path.isabs(args.trace_dir) else os.path.join(root, args.trace_dir)
    runs_file = os.path.join(root, "runs.jsonl") if not args.runs_file else (
        args.runs_file if os.path.isabs(args.runs_file) else os.path.join(root, args.runs_file)
    )
    logs_value = args.logs_dir or "logs"
    logs_dir = logs_value if os.path.isabs(logs_value) else os.path.join(root, logs_value)
    if args.ui != "none":
        Path(logs_dir).mkdir(parents=True, exist_ok=True)
    runs_path = None if args.no_jsonl else runs_file
    runs_discard_path = runs_file

    scenario = load_scenario(root, args.scenario)
    errors = validate_scenario(scenario, root, expected_id=args.scenario)
    if errors:
        print("Invalid scenario:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        sys.exit(2)

    try:
        topology_data = topology_tools.discover_topology()
        topology_tools.freeze_topology(topology_data)
        capability = topology_tools.capability_check(scenario, topology_data, args.mode)
        targets = scenario.get("target_nodes", [])
        resolved = topology_tools.resolve_targets(
            topology_data,
            targets,
            avoid_local=bool(scenario.get("avoid_local_target")),
        )
        min_targets = int(scenario.get("selector_requirements", {}).get("min_targets", 0))
        if len(resolved) < min_targets:
            raise RuntimeError(f"scenario requires at least {min_targets} target node(s); resolved {resolved}")
    except Exception as e:
        print(f"Topology preflight failed: {e}", file=sys.stderr)
        sys.exit(2)

    active_hash = active_topology_hash(args.db)
    if (args.resume or args.resume_of) and active_hash and active_hash != topology_data["topology_hash"]:
        print(
            f"refusing resume: active topology {active_hash} differs from discovered topology "
            f"{topology_data['topology_hash']}; discard stale state first",
            file=sys.stderr,
        )
        sys.exit(1)
    if not (args.resume or args.resume_of) and active_hash and active_hash != topology_data["topology_hash"]:
        print(
            f"topology changed from {active_hash} to {topology_data['topology_hash']}; "
            "discarding stale state",
            flush=True,
        )
        try:
            summary = discard_state(args.db, runs_discard_path, trace_dir)
        except Exception as exc:
            print(f"automatic state discard failed: {exc}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(summary), flush=True)

    store = SQLiteStore(args.db)
    spec_hash = store.hash_spec(scenario)
    spec_json = json.dumps(scenario, sort_keys=True)
    scenario_version = store.ensure_scenario(scenario["id"], spec_hash, spec_json)

    backend = build_backend(args.backend)

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
    run_env["CHAOS_TOPOLOGY_JSON"] = json.dumps(topology_data, sort_keys=True)
    run_env["TARGET_NODE"] = resolved[0] if resolved else ""
    run_env["TARGET_NODES"] = " ".join(resolved)

    config_hash_input = {
        "targets": resolved,
        "reset_cmd": reset_cmd,
        "backend": backend.name,
        "model": backend.model,
        "temperature": 0.2,
        "mode": args.mode,
        "approve": args.approve,
        "topology_hash": topology_data["topology_hash"],
        "impact_class": capability["impact_class"],
    }
    config_hash = store.hash_spec(config_hash_input)

    if not capability["eligible"]:
        reason = capability["unsupported_reason"] or "unsupported_topology"
        batch_id = store.create_batch(
            batch_id=args.batch_id,
            command=sys.argv,
            scenario_ids=[scenario["id"]],
            backend=backend.name,
            seed_start=args.seed,
            runs_requested=args.runs,
            reset_cmd=reset_cmd,
            config_hash=config_hash,
            topology_hash=topology_data["topology_hash"],
        )
        run_id = f"{scenario['id']}-{backend.name}-{args.seed}-unsupported-{uuid.uuid4().hex[:8]}"
        run_row = store.create_run(
            run_id,
            batch_id=batch_id,
            scenario_id=scenario["id"],
            scenario_version=scenario_version,
            spec_hash=spec_hash,
            backend=backend.name,
            model=backend.model,
            temperature=0.2,
            seed=args.seed,
            status="pending",
            phase="queued",
            truth=scenario["root_cause"],
            config_hash=config_hash,
            topology_hash=topology_data["topology_hash"],
            interactive=args.approve == "prompt",
            launch_id=args.launch_id or args.batch_id,
            pid=os.getpid(),
            harness_git_sha=store.git_sha(),
        )
        _record_topology(store, run_id, topology_data, capability)
        rec = _terminal_record(run_id, scenario, backend, False, False, reason, reason)
        store.finish_run(run_id, rec, status="skipped_unsupported", end_reason=reason)
        if runs_path:
            store.export_all(runs_path, trace_dir)
        store.update_batch(batch_id, "complete")
        print(json.dumps(rec), flush=True)
        return

    if not LLMClient(backend).health():
        print(f"Backend {backend.name} not healthy", file=sys.stderr)
        sys.exit(1)

    batch_id = store.create_batch(
        batch_id=args.batch_id,
        command=sys.argv,
        scenario_ids=[scenario["id"]],
        backend=backend.name,
        seed_start=args.seed,
        runs_requested=args.runs,
        reset_cmd=reset_cmd,
        config_hash=config_hash,
        topology_hash=topology_data["topology_hash"],
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
            if row.get("topology_hash") and row["topology_hash"] != topology_data["topology_hash"]:
                print(
                    f"refusing resume {row['run_id']}: run topology {row['topology_hash']} "
                    f"differs from discovered topology {topology_data['topology_hash']}; "
                    "discard stale state first",
                    file=sys.stderr,
                )
                sys.exit(1)
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
                new_row = _abort_and_retry(
                    store, row, reason, config_hash, spec_hash, scenario_version,
                    topology_data["topology_hash"],
                )
                if args.ui == "none":
                    print(f"=== aborted {row['run_id']} ({reason}); retry {new_row['run_id']} ===", flush=True)
                _record_topology(store, new_row["run_id"], topology_data, capability)
                rec = _run_fresh(
                    store, new_row, scenario, backend, inject_cmd, verify_cmd,
                    reset_cmd, run_env, timeout, trace_dir, runs_path,
                    args.ui, logs_dir, args.mode, args.approve,
                )
            else:
                if args.ui == "none":
                    print(f"=== resume {row['run_id']} ===", flush=True)
                rec = _run_resume(
                    store, row, scenario, backend, root, trace_dir, runs_path,
                    args.ui, logs_dir, args.mode, args.approve,
                )
            print(json.dumps(rec), flush=True)
        store.update_batch(batch_id, "complete")
        return

    for i in range(args.runs):
        run_id = f"{scenario['id']}-{backend.name}-{args.seed + i}-{uuid.uuid4().hex[:8]}"
        if args.ui == "none":
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
            topology_hash=topology_data["topology_hash"],
            interactive=args.approve == "prompt",
            launch_id=args.launch_id or args.batch_id,
            pid=os.getpid(),
            harness_git_sha=store.git_sha(),
        )
        _record_topology(store, run_id, topology_data, capability)
        rec = _run_fresh(
            store, run_row, scenario, backend, inject_cmd, verify_cmd,
            reset_cmd, run_env, timeout, trace_dir, runs_path,
            args.ui, logs_dir, args.mode, args.approve,
        )
        print(json.dumps(rec), flush=True)

    store.update_batch(batch_id, "complete")


if __name__ == "__main__":
    main()
