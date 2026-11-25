from __future__ import annotations

import argparse
import json
import fcntl
import os
import shlex
import socket
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from llm.client import LLMClient
from llm.env import load_env
from llm.registry import AUTO_BACKEND, backend_choices, resolve_backend
from harness.loop import print_llm_message
from harness.reaper import abandon_run, reap_expired
from llm.response import LLMOutcome
from replay import write_replay as write_replay_file
from storage import sqlite_store as store_tools
from tools import cluster_config
from tools import topology as topology_tools
from tools.scenario_schema import load_scenario, scenario_ids, validate_scenario

load_env()


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
        "avoid_local_target": bool(scenario.get("avoid_local_target")),
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
    warnings: list[str] = []
    warning = store_tools.stale_topology_warning(db_path, topology_hash)
    if warning:
        warnings.append(warning)
    active = store_tools.active_run_rows(db_path)
    if active:
        ids = ", ".join(str(row.get("run_id")) for row in active[:5])
        suffix = "..." if len(active) > 5 else ""
        warnings.append(f"{len(active)} active run row(s) remain: {ids}{suffix}")
    return warnings


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


def _local_addresses() -> set[str]:
    addresses = {cluster_config.current_node(), socket.gethostname()}
    try:
        addresses.update(item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None))
    except OSError:
        pass
    try:
        output = subprocess.check_output(["hostname", "-I"], text=True, timeout=5)
        addresses.update(output.split())
    except Exception:
        pass
    return addresses


def _ssh_preflight(hosts: list[str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    local = _local_addresses()
    for host in hosts:
        if host in local:
            results.append({"host": host, "ok": True, "detail": "local host", "local": True})
            continue
        cmd = [
            "ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10", f"{cluster_config.ssh_user()}@{host}", "true",
        ]
        key = cluster_config.ssh_key()
        if key:
            cmd[1:1] = ["-i", key]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            ok = proc.returncode == 0
            detail = proc.stderr.strip() or proc.stdout.strip()
        except Exception as exc:
            ok = False
            detail = str(exc)
        results.append({"host": host, "ok": ok, "detail": detail[:500]})
    return results


@contextmanager
def _rebuild_lock(conf_hash: str):
    cache_dir = Path.home() / ".cache" / "k8s-chaos-harness"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"rebuild-{conf_hash[:16]}.lock"
    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield path
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _setup_topology_warnings(setup: dict[str, Any], topology_data: dict[str, Any]) -> list[str]:
    configured = set(setup.get("masters") or [])
    live = {
        str(detail.get("ip"))
        for detail in topology_data.get("node_details", [])
        if detail.get("ip")
    }
    if not configured or not live:
        return []
    missing = sorted(configured - live)
    extra = sorted(live - configured)
    warnings: list[str] = []
    if missing:
        warnings.append("setup.conf masters absent from live topology: " + ", ".join(missing))
    if extra:
        warnings.append("live control-plane IPs absent from setup.conf: " + ", ".join(extra))
    return warnings


def _menu_script(summaries: list[dict[str, Any]]) -> str:
    ids = " ".join(["ping", *(shlex.quote(str(item["id"])) for item in summaries)])
    labels = " ".join(
        [shlex.quote("Ping LLM (auto fallback)"), *(shlex.quote(str(item["label"])) for item in summaries)]
    )
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
        'printf "Action [1-%d]: " "${#labels[@]}"',
        "read -r choice",
        'if ! [[ "$choice" =~ ^[0-9]+$ ]] || (( choice < 1 || choice > ${#labels[@]} )); then',
        '  echo "invalid selection" >&2',
        "  exit 2",
        "fi",
        'id="${ids[$((choice - 1))]}"',
        'cd "$ROOT"',
        'if [[ "$id" == "ping" ]]; then',
        '  exec ./chaos ping "$@"',
        "fi",
        'exec ./chaos run --scenario "$id" --backend "$BACKEND" "$@"',
        "",
    ]
    return "\n".join(lines)


def _ping(backend_name: str, message: str | None, verbose: bool = False) -> int:
    try:
        backend = resolve_backend(backend_name)
    except ValueError as exc:
        print(f"LLM configuration error: {exc}", file=sys.stderr)
        return 1
    client = LLMClient(backend)
    if not client.health():
        hints = {
            "ollama": "OLLAMA_BASE_URL, OLLAMA_MODEL, OLLAMA_API_KEY",
            "groq": "GROQ_API_KEY, GROQ_MODEL",
            "openrouter": "OPENROUTER_API_KEY, OPENROUTER_MODEL",
            "cloudflare": "CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_AUTH_TOKEN, CLOUDFLARE_MODEL",
        }
        chain = " -> ".join(candidate.name for candidate in [backend, *backend.fallbacks])
        print(f"No reachable LLM endpoint in chain: {chain}.", file=sys.stderr)
        print(f"LLM endpoint is not reachable for backend {backend.name} ({backend.base_url}).", file=sys.stderr)
        hint = hints.get(backend.name, "CHAOS_LLM_ENDPOINTS")
        print(f"Set {hint} in .env, then run: ./chaos ping --backend {backend.name}", file=sys.stderr)
        return 1
    if message is None:
        try:
            message = input("Say something to the LLM [default: Reply with exactly: pong]: ").strip()
        except EOFError:
            message = ""
    message = message or "Reply with exactly: pong"
    messages = [
        {"role": "system", "content": "You are a concise assistant. Reply directly to the user."},
        {"role": "user", "content": message},
    ]
    if verbose:
        print("=== ping conversation ===", flush=True)
        for item in messages:
            print_llm_message("request", 1, item)
    print(
        f"Waiting for {backend.name} / {backend.model}; the first call may load the model...",
        file=sys.stderr,
    )
    outcome = LLMOutcome.invoke(
        client,
        messages,
        temperature=0,
        max_tokens=256,
        timeout=180,
        retries=0,
    )
    if outcome.failed:
        print(f"LLM ping failed: {outcome.result.error}", file=sys.stderr)
        return 1
    response = outcome.result.unwrap()
    if verbose:
        print_llm_message("response", 1, response.primary.to_dict())
    content = (response.primary.content or "").strip()
    if not content:
        choice = (response.raw.get("choices") or [{}])[0]
        reasoning = str((choice.get("message") or {}).get("reasoning") or "").strip()
        content = f"(no content; reasoning: {reasoning[:200]})" if reasoning else "(empty response)"
    used_backend = response.backend or backend.name
    used_model = response.model or backend.model
    print(f"{used_backend} / {used_model}")
    if response.skipped_backends:
        print(f"skipped unhealthy: {', '.join(response.skipped_backends)}")
    if response.fallback_from:
        print(f"fallback from {' -> '.join(response.fallback_from)}")
    print(content)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        choices = backend_choices()
    except ValueError as exc:
        print(f"LLM configuration error: {exc}", file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(prog="chaos")
    ap.add_argument("--db", default=os.environ.get("CHECKPOINT_DB", str(ROOT / "results.sqlite3")))
    ap.add_argument("--runs-file", default=str(ROOT / "runs.jsonl"))
    ap.add_argument("--trace-dir", default=str(ROOT / "traces"))
    sub = ap.add_subparsers(dest="command", required=True)

    ping = sub.add_parser("ping", help="Check the configured LLM brain with a one-shot prompt; no cluster needed")
    ping.add_argument("message", nargs="?", default=None)
    ping.add_argument("--backend", default=os.environ.get("CHAOS_PING_BACKEND", AUTO_BACKEND), choices=choices)
    ping.add_argument("--verbose", action="store_true")

    state = sub.add_parser("state")
    state_sub = state.add_subparsers(dest="state_command", required=True)
    state_discard = state_sub.add_parser("discard")
    state_discard.add_argument("--yes", action="store_true")

    cluster = sub.add_parser("cluster")
    cluster_sub = cluster.add_subparsers(dest="cluster_command", required=True)
    cluster_nuke = cluster_sub.add_parser("nuke")
    cluster_nuke.add_argument("--yes", action="store_true")

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
    abort.add_argument("--abandon", action="store_true")

    run = sub.add_parser("run")
    run.add_argument("--scenario", required=True)
    run.add_argument("--backend", required=True, choices=choices)
    run.add_argument("--mode", choices=["benchmark", "repair"], default="benchmark")
    run.add_argument("--approve", choices=["policy", "prompt"], default="policy")
    run.add_argument("--runs", type=int, default=1)
    run.add_argument("--seed", type=int, default=0)
    run.add_argument("--batch-id", default=None)
    run.add_argument("--detach", action="store_true")
    run.add_argument("--ui", choices=["none", "plain", "live"], default="plain")
    run.add_argument("--logs-dir", default=str(ROOT / "logs"))
    run.add_argument("--resume-of", default=None)
    run.add_argument("--force-resume", action="store_true")
    run.add_argument("--no-jsonl", action="store_true")
    run.add_argument("--discard-stale", action="store_true")
    run.add_argument("--verbose", action="store_true")

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
    try:
        reaper_result = reap_expired(args.db)
    except Exception as exc:
        reaper_result = {"error": str(exc)}

    if args.command == "ping":
        return _ping(args.backend, args.message, args.verbose)

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
        ]
        if args.batch_id:
            cmd.extend(["--batch-id", args.batch_id])
        cmd.extend(["--launch-id", launch_id])
        if args.resume_of:
            cmd.extend(["--resume-of", args.resume_of])
        if args.force_resume:
            cmd.append("--force-resume")
        if args.no_jsonl:
            cmd.append("--no-jsonl")
        if args.discard_stale:
            cmd.append("--discard-stale")
        if args.verbose:
            cmd.append("--verbose")
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
        if args.abandon:
            if args.run_id == "all":
                print("--abandon requires a run_id", file=sys.stderr)
                return 2
            try:
                result = abandon_run(args.db, args.run_id)
            except Exception as exc:
                print(f"abandon failed: {exc}", file=sys.stderr)
                return 1
            _print_json(result)
            return 0
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

    if args.command == "cluster" and args.cluster_command == "nuke":
        if not args.yes:
            try:
                answer = input("Full rebuild from setup.conf destroys the current cluster. Continue? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer not in {"y", "yes"}:
                print("cluster nuke cancelled", file=sys.stderr)
                return 1
        setup = topology_tools.setup_conf_summary()
        if not setup["masters"]:
            print(f"no masters configured in {setup['path']}", file=sys.stderr)
            return 2
        try:
            with _rebuild_lock(setup["hash"]):
                preflight = _ssh_preflight(setup["masters"])
                failed = [item for item in preflight if not item["ok"]]
                if failed:
                    _print_json({
                        "status": "preflight_failed",
                        "setup_conf": setup,
                        "preflight": preflight,
                    })
                    return 2
                abort_summary = store_tools.abort_runs(
                    args.db, None, terminate=True, reason="cluster_nuke"
                )
                proc = subprocess.run(
                    ["bash", str(ROOT / "cluster" / "reset_cluster.sh")],
                    cwd=ROOT,
                    timeout=1800,
                    check=False,
                )
                if proc.returncode != 0:
                    _print_json({
                        "status": "rebuild_failed",
                        "returncode": proc.returncode,
                        "setup_conf": setup,
                        "preflight": preflight,
                        "runs_aborted": abort_summary["aborted"],
                        "state": "preserved",
                    })
                    return 1
                topology_tools.clear_frozen_topology()
                fixture = os.environ.pop("CHAOS_TOPOLOGY_JSON", None)
                try:
                    topology_data = topology_tools.discover_topology()
                finally:
                    if fixture is not None:
                        os.environ["CHAOS_TOPOLOGY_JSON"] = fixture
                warnings = _setup_topology_warnings(setup, topology_data)
                try:
                    cluster_config.cache_node_ips()
                except Exception as exc:
                    warnings.append(f"cannot refresh node IP cache after rebuild: {exc}")
                discard = store_tools.discard_state(
                    args.db, args.runs_file, args.trace_dir, cluster_config.CACHE_PATH
                )
                cluster_config.clear_ip_cache()
                try:
                    cluster_config.cache_node_ips()
                except Exception as exc:
                    warnings.append(f"cannot refresh node IP cache after discard: {exc}")
                _print_json({
                    "status": "ok",
                    "setup_conf": setup,
                    "preflight": preflight,
                    "runs_aborted": abort_summary["aborted"],
                    "topology": topology_tools.summary(topology_data),
                    "warnings": warnings,
                    "state": discard,
                })
                return 0
        except Exception as exc:
            print(f"cluster nuke failed: {exc}", file=sys.stderr)
            return 1

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
            "reaper": reaper_result,
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
