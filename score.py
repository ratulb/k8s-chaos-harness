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

ACCEPTABLE_BY_TRUTH = {
    "no_fault": set(),
    "etcd_member_unreachable": {"restart_member"},
    "etcd_config_error": {"restart_member", "member_remove_readd"},
    "etcd_member_data_loss": {"member_remove_readd", "kube_etcd_switch_rebuild"},
    "etcd_disk_exhaustion": {"restart_member", "kube_etcd_switch_rebuild"},
    "etcd_quorum_loss": {"restart_member", "snapshot_restore_full"},
}


def is_excluded(rec: dict, require_fault: bool = True) -> bool:
    reason = str(rec.get("end_reason") or "")
    if reason.startswith("provider_error") or reason in {"error", "fault_not_observed", "injection_failed"}:
        return True
    if require_fault and rec.get("fault_present") is not True:
        return True
    return False


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
    repair = rec.get("repair")
    if "repair_acceptable" in rec:
        return bool(rec["repair_acceptable"])
    truth = str(rec.get("truth") or "")
    if truth == "no_fault":
        return not repair
    return bool(repair and repair in ACCEPTABLE_BY_TRUTH.get(truth, set()))


def strict_success(rec: dict) -> bool:
    if is_excluded(rec):
        return False
    if not rec.get("diagnosis_correct") or not rec.get("verify_ok"):
        return False
    if rec.get("repair_forbidden") or rec.get("forbidden_reason") or rec.get("policy_violations"):
        return False
    no_fault = rec.get("truth") == "no_fault"
    if bool(rec.get("repair")) == no_fault:
        return False
    return acceptable_repair(rec)


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
    strict = sum(strict_success(r) for r in recs)
    return {
        "runs": len(recs),
        "diagnosis": round(diag / n, 3),
        "verify": round(verify / n, 3),
        "approval": round(approved / n, 3),
        "strict_loop_success": round(strict / n, 3),
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

    cols = ["runs", "diagnosis", "verify", "approval", "strict_loop_success", "loop_success", "policy_violations", "forbidden", "avg_steps", "avg_tokens"]
    print("| Group | " + " | ".join(cols) + " |")
    print("|---|" + "---|" * len(cols))
    for name, stats in [("overall", result["overall"])] + sorted(result["by_backend"].items()) + sorted(result["by_backend_scenario"].items()):
        label = name if isinstance(name, str) else "/".join(name)
        print(f"| {label} | " + " | ".join(str(stats[c]) for c in cols) + " |")


if __name__ == "__main__":
    main()
