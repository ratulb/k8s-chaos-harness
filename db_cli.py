#!/usr/bin/env python3
"""Command line utilities for the authoritative SQLite run store."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from storage.sqlite_store import SQLiteStore


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("CHECKPOINT_DB", "results.sqlite3"))
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("init")
    sub.add_parser("migrate")

    legacy = sub.add_parser("import-legacy")
    legacy.add_argument("--runs", default="runs.jsonl")
    legacy.add_argument("--trace-dir", default="traces")

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--strict", action="store_true")

    status = sub.add_parser("status")
    status.add_argument("--limit", type=int, default=10)

    export = sub.add_parser("export")
    export.add_argument("--runs", default="runs.jsonl")
    export.add_argument("--trace-dir", default="traces")

    prune = sub.add_parser("prune")
    prune.add_argument("--report-days", type=int, default=30)

    resume = sub.add_parser("resume")
    resume.add_argument("--scenario", required=True)
    resume.add_argument("--backend", required=True)
    resume.add_argument("--run-id", default=None)
    resume.add_argument("--resume-of", default=None)
    resume.add_argument("--force-resume", action="store_true")
    resume.add_argument("--no-jsonl", action="store_true")

    args = ap.parse_args()
    store = SQLiteStore(args.db)

    if args.command == "init":
        store.init_db()
        _print_json({"database": str(store.path), "initialized": True})
    elif args.command == "migrate":
        _print_json(store.migrate())
    elif args.command == "import-legacy":
        _print_json(store.import_legacy(args.runs, args.trace_dir))
    elif args.command == "doctor":
        report = store.doctor()
        _print_json(report)
        if args.strict and not report["ok"]:
            raise SystemExit(1)
    elif args.command == "status":
        counts = store.doctor()["counts"]
        with store._connect() as conn:
            recent = [dict(row) for row in conn.execute(
                """SELECT run_id, scenario_id, backend, status, phase, end_reason,
                          resume_count, steps, tokens_used, updated_at,
                          topology_hash, interactive, launch_id, pid
                   FROM runs ORDER BY updated_at DESC LIMIT ?""",
                (args.limit,),
            )]
        _print_json({"database": str(store.path), "counts": counts, "recent_runs": recent})
    elif args.command == "export":
        _print_json(store.export_all(args.runs, args.trace_dir))
    elif args.command == "prune":
        cutoff = store._now() - args.report_days * 86400
        with store._connect() as conn:
            cursor = conn.execute("DELETE FROM score_reports WHERE created_at < ?", (cutoff,))
            deleted = cursor.rowcount
            conn.execute("VACUUM")
        _print_json({"deleted_score_reports": deleted, "vacuumed": True})
    elif args.command == "resume":
        cmd = [
            sys.executable,
            str(Path(__file__).with_name("run_benchmark.py")),
            "--scenario", args.scenario,
            "--backend", args.backend,
            "--db", args.db,
            "--resume",
        ]
        if args.run_id:
            cmd.extend(["--resume-of", args.run_id])
        if args.force_resume:
            cmd.append("--force-resume")
        if args.no_jsonl:
            cmd.append("--no-jsonl")
        raise SystemExit(subprocess.call(cmd))


if __name__ == "__main__":
    main()
