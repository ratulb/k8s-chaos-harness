from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from harness.journal import Journal
from harness.machines.run import RunPhase
from harness.run_runtime import RunRuntime
from storage.sqlite_store import SQLiteStore, _pid_alive, read_only_rows


def _live_owner(row: dict[str, Any]) -> bool:
    return _pid_alive(row.get("pid"))


def reap_expired(db_path: str, *, detected_by: str = "reaper", now: float | None = None) -> dict[str, Any]:
    if not Path(db_path).exists():
        return {"observed": [], "detected_by": detected_by}
    now = time.time() if now is None else now
    rows = read_only_rows(db_path, "SELECT * FROM runs")
    store = SQLiteStore(db_path)
    observed: list[str] = []
    try:
        for row in rows:
            status = str(row.get("status") or "")
            if status in {"finished", "aborted", "skipped_unsupported"}:
                continue
            expires_at = row.get("lease_expires_at")
            if expires_at is None or float(expires_at) > now:
                continue
            if _live_owner(dict(row)):
                continue
            run_id = str(row["run_id"])
            run = RunRuntime.from_events(run_id, store.load_events(run_id))
            if run.state.phase in {
                RunPhase.FINISHED,
                RunPhase.ABORTED,
                RunPhase.SKIPPED_UNSUPPORTED,
            }:
                continue
            previous = run.state.previous_state if run.state.phase is RunPhase.INTERRUPTED else run.state.phase
            lease_expires_at = run.state.lease_expires_at or float(expires_at)
            journal = Journal(run_id, store)
            run.log = journal.event
            run.lease_expired(previous, lease_expires_at, detected_by=detected_by)
            store.release_lease(run_id)
            observed.append(run_id)
        return {"observed": observed, "detected_by": detected_by}
    finally:
        store.close()


def abandon_run(db_path: str, run_id: str, *, actor: str = "operator") -> dict[str, Any]:
    store = SQLiteStore(db_path)
    try:
        row = store.get_run(run_id)
        if not row:
            raise ValueError(f"unknown run: {run_id}")
        run = RunRuntime.from_events(run_id, store.load_events(run_id))
        if run.state.phase is not RunPhase.INTERRUPTED:
            raise ValueError(f"run {run_id} is not interrupted")
        run.log = Journal(run_id, store).event
        run.abandon(actor=actor)
        store.finish_run(
            run_id,
            status=run.state.status,
            end_reason=run.end_reason,
            state=store.get_state(run_id),
        )
        store.release_lease(run_id)
        return {"run_id": run_id, "status": run.state.status, "end_reason": run.end_reason, "actor": actor}
    finally:
        store.close()
