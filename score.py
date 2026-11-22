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
from common.scoring import ACCEPTABLE_BY_TRUTH, SCORER_VERSION, Score, is_excluded
from storage.sqlite_store import SQLiteStore


def load(
    path: str,
    include_resumed: bool = False,
    include_interactive: bool = False,
    include_provider_errors: bool = False,
    include_unverified_faults: bool = False,
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
        if "approval_granted" not in rec:
            continue
        if not include_resumed and rec.get("resume_count", 0):
            continue
        if not include_interactive and rec.get("interactive"):
            continue
        if rec.get("status") == "skipped_unsupported" or "unsupported" in str(rec.get("end_reason", "")):
            continue
        if not include_provider_errors and is_excluded(rec, require_fault=False):
            continue
        if not include_unverified_faults and rec.get("fault_present") is not True:
            continue
        out.append(rec)
    return out


def acceptable_repair(rec: dict) -> bool:
    return Score.from_record(rec).repair_acceptable


def strict_success(rec: dict) -> bool:
    return Score.from_record(rec).strict_loop_success


def summarize(recs: list[dict]) -> dict:
    n = len(recs) or 1
    scores = [Score.from_record(rec) for rec in recs]
    return {
        "runs": len(recs),
        "diagnosis": round(sum(s.diagnosis_correct for s in scores) / n, 3),
        "first_attempt_diagnosis": round(sum(s.diagnosis_first_attempt_correct for s in scores) / n, 3),
        "verify": round(sum(s.recovered for s in scores) / n, 3),
        "approval": round(sum(bool(r.get("approval_granted")) for r in recs) / n, 3),
        "strict_loop_success": round(sum(s.strict_loop_success for s in scores) / n, 3),
        "loop_success": round(sum(s.loop_success for s in scores) / n, 3),
        "policy_violations": round(sum(bool(s.policy_violations) for s in scores) / n, 3),
        "forbidden": round(sum(s.repair_forbidden for s in scores) / n, 3),
        "budget_exhausted": round(sum(s.end_reason in {"budget_steps", "budget_tokens"} for s in scores) / n, 3),
        "avg_diagnosis_attempts": round(sum(s.diagnosis_attempts for s in scores) / n, 2),
        "avg_steps": round(sum(r.get("steps", 0) for r in recs) / n, 1),
        "avg_tokens": round(sum(r.get("tokens_used", 0) for r in recs) / n, 1),
        "scorer_version": SCORER_VERSION,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="runs.jsonl")
    ap.add_argument("--db", default=None)
    ap.add_argument("--format", choices=["json", "md"], default="md")
    ap.add_argument("--include-resumed", action="store_true")
    ap.add_argument("--include-interactive", action="store_true")
    ap.add_argument("--include-provider-errors", action="store_true")
    ap.add_argument("--include-unverified-faults", action="store_true")
    args = ap.parse_args()

    if args.db:
        with SQLiteStore(args.db) as store:
            recs = store.load_records(
                include_resumed=args.include_resumed,
                include_interactive=args.include_interactive,
                include_provider_errors=args.include_provider_errors,
                include_unverified_faults=args.include_unverified_faults,
            )
    else:
        recs = load(
            args.runs,
            include_resumed=args.include_resumed,
            include_interactive=args.include_interactive,
            include_provider_errors=args.include_provider_errors,
            include_unverified_faults=args.include_unverified_faults,
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

    cols = ["runs", "diagnosis", "first_attempt_diagnosis", "verify", "approval", "strict_loop_success", "loop_success", "policy_violations", "forbidden", "budget_exhausted", "avg_diagnosis_attempts", "avg_steps", "avg_tokens"]
    print("| Group | " + " | ".join(cols) + " |")
    print("|---|" + "---|" * len(cols))
    for name, stats in [("overall", result["overall"])] + sorted(result["by_backend"].items()) + sorted(result["by_backend_scenario"].items()):
        label = name if isinstance(name, str) else "/".join(name)
        print(f"| {label} | " + " | ".join(str(stats[c]) for c in cols) + " |")


if __name__ == "__main__":
    main()
