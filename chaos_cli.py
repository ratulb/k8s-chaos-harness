from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from llm.backends import BACKEND_NAMES
from replay import write_replay as write_replay_file
from storage import sqlite_store as store_tools
from tools import cluster_config
from tools import topology as topology_tools
from tools.scenario_schema import load_scenario, scenario_ids, validate_scenario


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _summary(scenario: dict[str, Any]) -> dict[str, Any]:
    menu = scenario.get("menu", {})
    return {
        "id": scenario.get("id"),
        "label": menu.get("label", scenario.get("id")),
        "summary": menu.get("summary", ""),
        "risk": scenario.get("risk"),
        "impact_class": scenario.get("impact_class"),
        "min_etcd_members": scenario.get("min_etcd_members"),
        "target_nodes": scenario.get("target_nodes", []),
    }


def _summaries(ids: list[str]) -> list[dict[str, Any]]:
    return [_summary(load_scenario(ROOT, scenario_id)) for scenario_id in ids]


def _validation(ids: list[str]) -> dict[str, Any]:
    errors: dict[str, list[str]] = {}
    for scenario_id in ids:
        scenario = load_scenario(ROOT, scenario_id)
        found = validate_scenario(scenario, ROOT, expected_id=scenario_id)
        if found:
            errors[scenario_id] = found
    return {"ok": not errors, "scenarios": len(ids), "errors": errors}


def _stale_warnings(db_path: str, topology_hash: str | None = None) -> list[str]:
    if not Path(db_path).exists():
        return ["no active database; read-only view has no stored topology to compare"]
    if topology_hash is None:
        try:
            topology_hash = topology_tools.discover_topology()["topology_hash"]
        except Exception as exc:
            return [f"cannot discover topology for stale check: {exc}"]
    warning = store_tools.stale_topology_warning(db_path, topology_hash)
    return [warning] if warning else []


def _json_field(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _parse_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for event in events:
        if "payload_json" in event:
            event["payload"] = _json_field(event.pop("payload_json", None))
    return events


def _print_attach_items(events: list[dict[str, Any]], chunks: list[dict[str, Any]], as_json: bool) -> None:
    for event in _parse_events(events):
        if as_json:
            _print_json({"kind": "event", **event})
        else:
            print(f"{event['seq']:>6}  {event['type']}  {json.dumps(event.get('payload'), sort_keys=True)}")
    for chunk in chunks:
        if as_json:
            _print_json({"kind": "ui", **chunk})
        else:
            print(f"ui {chunk['seq']:>6}  {chunk['kind']}: {chunk['text']}")


def _menu_script(summaries: list[dict[str, Any]]) -> str:
    ids = " ".join(shlex.quote(str(item["id"])) for item in summaries)
    labels = " ".join(shlex.quote(str(item["label"])) for item in summaries)
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        'ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        'BACKEND="${BACKEND:-ollama}"',
        f"ids=({ids})",
        f"labels=({labels})",
        "",
        "for i in \"${!labels[@]}\"; do",
        '  printf "%2d) %s\\n" "$((i + 1))" "${labels[$i]}"',
        "done",
        'printf "Scenario [1-%d]: " "${#labels[@]}"',
        "read -r choice",
        'if ! [[ "$choice" =~ ^[0-9]+$ ]] || (( choice < 1 || choice > ${#labels[@]} )); then',
        '  echo "invalid scenario selection" >&2',
        "  exit 2",
        "fi",
        'id="${ids[$((choice - 1))]}"',
        'exec bash "$ROOT/run_benchmark.sh" --scenario "$id" --backend "$BACKEND" "$@"',
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="chaos")
    ap.add_argument("--db", default=os.environ.get("CHECKPOINT_DB", str(ROOT / "results.sqlite3")))
    ap.add_argument("--runs-file", default=str(ROOT / "runs.jsonl"))
    ap.add_argument("--trace-dir", default=str(ROOT / "traces"))
    sub = ap.add_subparsers(dest="command", required=True)

    state = sub.add_parser("state")
    state_sub = state.add_subparsers(dest="state_command", required=True)
    state_discard = state_sub.add_parser("discard")
    state_discard.add_argument("--yes", action="store_true")

    status = sub.add_parser("status")
    status.add_argument("--limit", type=int, default=10)
    status.add_argument("--json", action="store_true")

    replay_cmd = sub.add_parser("replay")
    replay_cmd.add_argument("--output", default="replay.html")
    replay_cmd.add_argument("--include-interactive", action="store_true")

    attach = sub.add_parser("attach")
    attach.add_argument("run_id")
    attach.add_argument("--after-seq", type=int, default=0)
    attach.add_argument("--follow", action="store_true")
    attach.add_argument("--interval", type=float, default=1.0)
    attach.add_argument("--json", action="store_true")

    abort = sub.add_parser("abort")
    abort.add_argument("run_id")
    abort.add_argument("--yes", action="store_true")
    abort.add_argument("--no-kill", action="store_true")

    run = sub.add_parser("run")
    run.add_argument("--scenario", required=True)
    run.add_argument("--backend", required=True, choices=list(BACKEND_NAMES))
    run.add_argument("--mode", choices=["benchmark", "repair"], default="benchmark")
    run.add_argument("--approve", choices=["policy", "prompt"], default="policy")
    run.add_argument("--runs", type=int, default=1)
    run.add_argument("--seed", type=int, default=0)
    run.add_argument("--detach", action="store_true")
    run.add_argument("--ui", choices=["none", "plain", "live"], default="plain")
    run.add_argument("--logs-dir", default=str(ROOT / "logs"))
    run.add_argument("--resume-of", default=None)
    run.add_argument("--force-resume", action="store_true")
    run.add_argument("--no-jsonl", action="store_true")

    target = sub.add_parser("target")
    target_sub = target.add_subparsers(dest="target_command", required=True)
    target_show = target_sub.add_parser("show")
    target_show.add_argument("--json", action="store_true")
    target_show.add_argument("--mode", choices=["benchmark", "repair"], default="benchmark")

    scenario = sub.add_parser("scenario")
    scenario_sub = scenario.add_subparsers(dest="scenario_command", required=True)

    scenario_list = scenario_sub.add_parser("list")
    scenario_list.add_argument("--json", action="store_true")
    scenario_list.add_argument("--risk", choices=["read-only", "disruptive", "destructive"])

    scenario_show = scenario_sub.add_parser("show")
    scenario_show.add_argument("scenario_id")
    scenario_show.add_argument("--json", action="store_true")

    scenario_validate = scenario_sub.add_parser("validate")
    scenario_validate.add_argument("scenario_id", nargs="?")
    scenario_validate.add_argument("--json", action="store_true")

    scenario_menu = scenario_sub.add_parser("menu")
    scenario_menu.add_argument("--output", default="menu.sh")

    args = ap.parse_args(argv)

    if args.command == "run":
        launch_id = f"launch-{uuid.uuid4().hex[:8]}"
        logs_dir = Path(args.logs_dir)
        if not logs_dir.is_absolute():
            logs_dir = ROOT / logs_dir
        logs_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable, str(ROOT / "run_benchmark.py"),
            "--scenario", args.scenario,
            "--backend", args.backend,
            "--mode", args.mode,
            "--approve", args.approve,
            "--runs", str(args.runs),
            "--seed", str(args.seed),
            "--db", args.db,
            "--runs-file", args.runs_file,
            "--trace-dir", args.trace_dir,
            "--ui", args.ui,
            "--logs-dir", str(logs_dir),
            "--launch-id", launch_id,
        ]
        if args.resume_of:
            cmd.extend(["--resume-of", args.resume_of])
        if args.force_resume:
            cmd.append("--force-resume")
        if args.no_jsonl:
            cmd.append("--no-jsonl")
        env = os.environ.copy()
        env["CHECKPOINT_DB"] = args.db
        if not args.detach:
            return subprocess.call(cmd, cwd=ROOT, env=env)
        log_path = logs_dir / f"{launch_id}.log"
        with open(log_path, "ab", buffering=0) as handle:
            proc = subprocess.Popen(
                cmd,
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        _print_json({
            "launch_id": launch_id,
            "pid": proc.pid,
            "log": str(log_path),
            "command": cmd,
        })
        return 0

    if args.command == "abort":
        if args.run_id == "all" and not args.yes:
            try:
                answer = input(f"Abort all active runs in {args.db}? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer not in {"y", "yes"}:
                print("abort cancelled", file=sys.stderr)
                return 1
        targets = None if args.run_id == "all" else [args.run_id]
        try:
            summary = store_tools.abort_runs(
                args.db, targets, terminate=not args.no_kill, reason="operator_abort"
            )
        except Exception as exc:
            print(f"abort failed: {exc}", file=sys.stderr)
            return 1
        _print_json(summary)
        return 0

    if args.command == "state" and args.state_command == "discard":
        if not args.yes:
            try:
                answer = input(f"Discard state for {args.db}? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer not in {"y", "yes"}:
                print("state discard cancelled", file=sys.stderr)
                return 1
        try:
            summary = store_tools.discard_state(
                args.db, args.runs_file, args.trace_dir, cluster_config.CACHE_PATH
            )
            cluster_config.clear_ip_cache()
        except Exception as exc:
            print(f"state discard failed: {exc}", file=sys.stderr)
            return 1
        _print_json(summary)
        return 0

    if args.command == "replay":
        try:
            payload = write_replay_file(
                args.db, args.output, include_interactive=args.include_interactive
            )
        except Exception as exc:
            print(f"replay failed: {exc}", file=sys.stderr)
            return 1
        _print_json(payload)
        return 0

    if args.command == "status":
        warnings = _stale_warnings(args.db)
        payload = {
            "database": args.db,
            "counts": store_tools.read_only_counts(args.db),
            "recent_runs": store_tools.read_only_rows(
                args.db,
                """SELECT run_id, scenario_id, backend, status, phase, end_reason,
                          resume_count, steps, tokens_used, updated_at,
                          topology_hash, interactive, launch_id, pid
                   FROM runs ORDER BY updated_at DESC LIMIT ?""",
                (args.limit,),
            ),
            "warnings": warnings,
        }
        if args.json:
            _print_json(payload)
        else:
            print(f"database: {payload['database']}")
            print("counts: " + ", ".join(f"{key}={value}" for key, value in payload["counts"].items()))
            for warning in warnings:
                print(f"warning: {warning}")
            for run in payload["recent_runs"]:
                print(
                    f"{run['run_id']}  {run['status']}/{run['phase']}  "
                    f"{run['backend']}  {run['scenario_id']}  topology={run['topology_hash']}"
                )
        return 0

    if args.command == "attach":
        run_rows = store_tools.read_only_rows(
            args.db, "SELECT * FROM runs WHERE run_id=?", (args.run_id,)
        )
        if not run_rows:
            print(f"unknown run {args.run_id!r}", file=sys.stderr)
            return 1
        run = run_rows[0]
        warnings = _stale_warnings(args.db)
        if not args.follow:
            events = store_tools.read_only_rows(
                args.db,
                "SELECT * FROM trace_events WHERE run_id=? AND seq>? ORDER BY seq",
                (args.run_id, args.after_seq),
            )
            stream = store_tools.read_only_rows(
                args.db,
                "SELECT * FROM ui_stream WHERE run_id=? AND seq>? ORDER BY seq",
                (args.run_id, args.after_seq),
            )
            payload = {
                "database": args.db,
                "run": run,
                "events": _parse_events(events),
                "ui_stream": stream,
                "warnings": warnings,
            }
            if args.json:
                _print_json(payload)
            else:
                print(f"run: {run['run_id']}  {run['status']}/{run['phase']}")
                for warning in warnings:
                    print(f"warning: {warning}")
                _print_attach_items(events, stream, False)
            return 0

        if args.json:
            _print_json({"kind": "run", "run": run, "warnings": warnings})
        else:
            print(f"following {run['run_id']} ({run['status']}/{run['phase']})")
            for warning in warnings:
                print(f"warning: {warning}")
        event_seq = args.after_seq
        ui_seq = args.after_seq
        terminal = {"finished", "aborted", "skipped_unsupported"}
        try:
            while True:
                events = store_tools.read_only_rows(
                    args.db,
                    "SELECT * FROM trace_events WHERE run_id=? AND seq>? ORDER BY seq",
                    (args.run_id, event_seq),
                )
                chunks = store_tools.read_only_rows(
                    args.db,
                    "SELECT * FROM ui_stream WHERE run_id=? AND seq>? ORDER BY seq",
                    (args.run_id, ui_seq),
                )
                _print_attach_items(events, chunks, args.json)
                if events:
                    event_seq = max(int(item["seq"]) for item in events)
                if chunks:
                    ui_seq = max(int(item["seq"]) for item in chunks)
                status_rows = store_tools.read_only_rows(
                    args.db, "SELECT status, phase FROM runs WHERE run_id=?", (args.run_id,)
                )
                status = status_rows[0]["status"] if status_rows else "unknown"
                if status in terminal and not events and not chunks:
                    break
                time.sleep(max(0.1, args.interval))
        except KeyboardInterrupt:
            print("\nattach stopped", file=sys.stderr)
        return 0

    if args.command == "target":
        try:
            topology_data = topology_tools.discover_topology()
        except Exception as exc:
            print(f"cannot discover topology: {exc}", file=sys.stderr)
            return 2
        capability = {
            scenario_id: topology_tools.capability_check(
                load_scenario(ROOT, scenario_id),
                topology_data,
                args.mode,
            )
            for scenario_id in scenario_ids(ROOT)
        }
        warnings = list(topology_data["warnings"])
        warnings.extend(_stale_warnings(args.db, topology_data["topology_hash"]))
        if args.json:
            _print_json({
                "topology": topology_tools.summary(topology_data),
                "capability": capability,
                "warnings": warnings,
            })
            return 0
        print(f"source: {topology_data['source']} ({topology_data['confidence']})")
        print(f"hash: {topology_data['topology_hash']}")
        print(f"control planes: {', '.join(topology_data['control_planes'])}")
        print(f"etcd members: {topology_data['etcd_member_count']}")
        print(f"quorum: {topology_data['quorum']} of {topology_data['etcd_member_count']}")
        print(f"quorum-loss targets: {', '.join(topology_data['quorum_loss_nodes'])}")
        for result in capability.values():
            warnings.extend(result["warning"])
        for warning in dict.fromkeys(warnings):
            print(f"warning: {warning}")
        for scenario_id, result in capability.items():
            status = "ok" if result["eligible"] else "unsupported"
            print(f"scenario {scenario_id}: {status} ({result['impact_class']})")
            if result["unsupported_reason"]:
                print(f"  reason: {result['unsupported_reason']}")
        return 0

    if args.command == "scenario" and args.scenario_command == "list":
        ids = scenario_ids(ROOT)
        summaries = _summaries(ids)
        if args.risk:
            summaries = [item for item in summaries if item["risk"] == args.risk]
        if args.json:
            _print_json(summaries)
        else:
            for item in summaries:
                print(f"{item['id']}  [{item['risk']}]  {item['label']}: {item['summary']}")
        return 0

    if args.command == "scenario" and args.scenario_command == "show":
        if args.scenario_id not in scenario_ids(ROOT):
            print(f"unknown scenario {args.scenario_id!r}", file=sys.stderr)
            return 2
        scenario_data = load_scenario(ROOT, args.scenario_id)
        warnings = _stale_warnings(args.db)
        if args.json:
            _print_json({**scenario_data, "warnings": warnings})
        else:
            print(json.dumps(scenario_data, indent=2, sort_keys=True))
            for warning in warnings:
                print(f"warning: {warning}")
        return 0

    if args.command == "scenario" and args.scenario_command == "validate":
        if args.scenario_id:
            if args.scenario_id not in scenario_ids(ROOT):
                print(f"unknown scenario {args.scenario_id!r}", file=sys.stderr)
                return 2
            errors = validate_scenario(
                load_scenario(ROOT, args.scenario_id),
                ROOT,
                expected_id=args.scenario_id,
            )
            payload = {"ok": not errors, "id": args.scenario_id, "errors": errors}
            _print_json(payload)
            return 0 if not errors else 1
        payload = _validation(scenario_ids(ROOT))
        _print_json(payload)
        return 0 if payload["ok"] else 1

    if args.command == "scenario" and args.scenario_command == "menu":
        summaries = _summaries(scenario_ids(ROOT))
        output = Path(args.output)
        if not output.is_absolute():
            output = ROOT / output
        output.write_text(_menu_script(summaries), encoding="utf-8")
        output.chmod(0o755)
        _print_json({"menu": str(output), "scenarios": len(summaries)})
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
