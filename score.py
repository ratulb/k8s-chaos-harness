#!/usr/bin/env python3
"""Aggregate authoritative SQLite runs, or legacy JSONL, into comparison tables."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from storage.sqlite_store import SQLiteStore


def load(
    path: str,
    include_resumed: bool = False,
    include_interactive: bool = False,
) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        # Pre-gate exploratory records predate approval_granted.
        if "approval_granted" not in rec:
            continue
        # Resumed runs contain interventions and are excluded from headline rates.
        if not include_resumed and rec.get("resume_count", 0):
            continue
        if not include_interactive and rec.get("interactive"):
            continue
        if rec.get("status") == "skipped_unsupported" or "unsupported" in str(rec.get("end_reason", "")):
            continue
        out.append(rec)
    return out


def summarize(recs: list[dict]) -> dict:
    n = len(recs) or 1
    diag = sum(bool(r.get("diagnosis_correct")) for r in recs)
    verify = sum(bool(r.get("verify_ok")) for r in recs)
    approved = sum(bool(r.get("approval_granted")) for r in recs)
    forbidden = sum(bool(r.get("forbidden_reason")) for r in recs)
    policy = sum(bool(r.get("policy_violations")) for r in recs)
    success = 0
    for r in recs:
        no_fault = r.get("truth") == "no_fault"
        repaired = bool(r.get("repair"))
        if r.get("diagnosis_correct") and r.get("verify_ok") and (repaired != no_fault):
            success += 1
    return {
        "runs": len(recs),
        "diagnosis": round(diag / n, 3),
        "verify": round(verify / n, 3),
        "approval": round(approved / n, 3),
        "loop_success": round(success / n, 3),
        "policy_violations": round(policy / n, 3),
        "forbidden": round(forbidden / n, 3),
        "avg_steps": round(sum(r.get("steps", 0) for r in recs) / n, 1),
        "avg_tokens": round(sum(r.get("tokens_used", 0) for r in recs) / n, 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="runs.jsonl")
    ap.add_argument("--db", default=None)
    ap.add_argument("--format", choices=["json", "md"], default="md")
    ap.add_argument("--include-resumed", action="store_true")
    ap.add_argument("--include-interactive", action="store_true")
    args = ap.parse_args()

    if args.db:
        with SQLiteStore(args.db) as store:
            recs = store.load_records(
                include_resumed=args.include_resumed,
                include_interactive=args.include_interactive,
            )
    else:
        recs = load(
            args.runs,
            include_resumed=args.include_resumed,
            include_interactive=args.include_interactive,
        )

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    by_backend: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        groups[(r.get("backend", "?"), r.get("scenario", "?"))].append(r)
        by_backend[r.get("backend", "?")].append(r)

    result = {
        "overall": summarize(recs),
        "by_backend": {k: summarize(v) for k, v in sorted(by_backend.items())},
        "by_backend_scenario": {f"{b}/{s}": summarize(v) for (b, s), v in sorted(groups.items())},
    }
    if args.format == "json":
        print(json.dumps(result, indent=2))
        return

    cols = ["runs", "diagnosis", "verify", "approval", "loop_success", "policy_violations", "forbidden", "avg_steps", "avg_tokens"]
    print("| Group | " + " | ".join(cols) + " |")
    print("|---|" + "---|" * len(cols))
    for name, stats in [("overall", result["overall"])] + sorted(result["by_backend"].items()) + sorted(result["by_backend_scenario"].items()):
        label = name if isinstance(name, str) else "/".join(name)
        print(f"| {label} | " + " | ".join(str(stats[c]) for c in cols) + " |")


if __name__ == "__main__":
    main()
