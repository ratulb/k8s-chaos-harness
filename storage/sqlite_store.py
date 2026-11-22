"""SQLite-backed authoritative store for benchmark runs.

The event tables are the source of truth: messages, tool_calls, llm_requests
and trace_events are append-only. A runs.state_json blob is only a cache that
is rebuilt from the log whenever its seq marker is stale.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from common.scoring import SCORER_VERSION
from common.vocab import DESTRUCTIVE_TOOLS, DIAGNOSIS_TOOL, REPAIR_TOOLS
from harness.message import Message
from tools.naming import LEGACY_BACKEND_TOKENS, normalize_backend, normalize_repair_name

SCHEMA_VERSION = 5

RUN_COLUMNS = {
    "run_id": "TEXT PRIMARY KEY",
    "batch_id": "TEXT",
    "attempt": "INTEGER NOT NULL DEFAULT 1",
    "resume_of": "TEXT",
    "resume_count": "INTEGER NOT NULL DEFAULT 0",
    "scenario_id": "TEXT NOT NULL",
    "scenario_version": "INTEGER NOT NULL DEFAULT 1",
    "spec_hash": "TEXT",
    "backend": "TEXT NOT NULL",
    "model": "TEXT",
    "quant": "TEXT",
    "temperature": "REAL",
    "seed": "INTEGER NOT NULL DEFAULT 0",
    "prompt_hash": "TEXT",
    "harness_git_sha": "TEXT",
    "status": "TEXT NOT NULL DEFAULT 'pending'",
    "phase": "TEXT NOT NULL DEFAULT 'queued'",
    "end_reason": "TEXT",
    "truth": "TEXT",
    "diagnosis": "TEXT",
    "diagnosis_raw": "TEXT",
    "diagnosis_correct": "INTEGER",
    "diagnosis_attempts": "INTEGER",
    "diagnosis_first_attempt_correct": "INTEGER",
    "approval_granted": "INTEGER",
    "repair": "TEXT",
    "repair_args_json": "TEXT",
    "policy_violations_json": "TEXT",
    "forbidden_reason": "TEXT",
    "reset_ok": "INTEGER",
    "baseline_ok": "INTEGER",
    "inject_ok": "INTEGER",
    "fault_present": "INTEGER DEFAULT 0",
    "verify_ok": "INTEGER",
    "verify_output": "TEXT",
    "steps": "INTEGER NOT NULL DEFAULT 0",
    "tokens_used": "INTEGER NOT NULL DEFAULT 0",
    "started_at": "REAL",
    "updated_at": "REAL NOT NULL",
    "finished_at": "REAL",
    "trace_path": "TEXT",
    "config_hash": "TEXT",
    "topology_hash": "TEXT",
    "interactive": "INTEGER NOT NULL DEFAULT 0",
    "launch_id": "TEXT",
    "pid": "INTEGER",
    "state_json": "TEXT",
    "state_seq": "INTEGER NOT NULL DEFAULT 0",
    "seq": "INTEGER NOT NULL DEFAULT 0",
    "lease_owner": "TEXT",
    "lease_expires_at": "REAL",
}

SCORE_COLUMNS = {
    "diagnosis_attempts": "INTEGER",
    "diagnosis_first_attempt_correct": "INTEGER",
    "strict_loop_success": "INTEGER",
}

SCORE_BOOL_FIELDS = {
    "diagnosis_first_attempt_correct",
    "repair_acceptable",
    "repair_forbidden",
    "strict_loop_success",
    "loop_success",
}

RUN_JSON_COLUMNS = {"repair_args_json", "policy_violations_json", "state_json"}
RUN_BOOL_COLUMNS = {
    "diagnosis_correct",
    "diagnosis_first_attempt_correct",
    "approval_granted",
    "reset_ok",
    "baseline_ok",
    "inject_ok",
    "fault_present",
    "verify_ok",
    "interactive",
}

RECORD_TO_COLUMN = {
    "run_id": "run_id",
    "scenario": "scenario_id",
    "backend": "backend",
    "truth": "truth",
    "diagnosis": "diagnosis",
    "diagnosis_raw": "diagnosis_raw",
    "diagnosis_correct": "diagnosis_correct",
    "diagnosis_attempts": "diagnosis_attempts",
    "diagnosis_first_attempt_correct": "diagnosis_first_attempt_correct",
    "approval_granted": "approval_granted",
    "repair": "repair",
    "repair_args": "repair_args_json",
    "policy_violations": "policy_violations_json",
    "forbidden_reason": "forbidden_reason",
    "reset_ok": "reset_ok",
    "baseline_ok": "baseline_ok",
    "inject_ok": "inject_ok",
    "fault_present": "fault_present",
    "verify_ok": "verify_ok",
    "verify_output": "verify_output",
    "end_reason": "end_reason",
    "steps": "steps",
    "tokens_used": "tokens_used",
    "trace": "trace_path",
}


def read_only_rows(path: str | Path, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    try:
        conn = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def active_topology_hash(db_path: str | Path) -> str | None:
    rows = read_only_rows(
        db_path,
        """SELECT topology_hash, updated_at FROM (
               SELECT topology_hash, updated_at FROM runs
               WHERE topology_hash IS NOT NULL AND topology_hash != ''
               UNION ALL
               SELECT topology_hash, created_at AS updated_at FROM batches
               WHERE topology_hash IS NOT NULL AND topology_hash != ''
           ) ORDER BY updated_at DESC LIMIT 1""",
    )
    return rows[0]["topology_hash"] if rows else None


def active_run_rows(db_path: str | Path) -> list[dict[str, Any]]:
    now = time.time()
    rows = read_only_rows(db_path, "SELECT * FROM runs")
    return [
        row for row in rows
        if row.get("status") in {"pending", "running", "interrupted"}
        or (row.get("lease_expires_at") is not None and row["lease_expires_at"] > now)
    ]


def read_only_counts(db_path: str | Path) -> dict[str, int]:
    tables = [
        "batches", "runs", "scenarios", "messages", "tool_calls", "llm_requests",
        "trace_events", "run_artifacts", "state_snapshots", "ui_stream",
        "diagnosis_attempts", "policy_violations", "scores", "score_reports",
    ]
    counts: dict[str, int] = {}
    for table in tables:
        rows = read_only_rows(db_path, f"SELECT COUNT(*) AS total FROM {table}")
        counts[table] = int(rows[0]["total"]) if rows else 0
    return counts


def stale_topology_warning(db_path: str | Path, topology_hash: str) -> str | None:
    active = active_topology_hash(db_path)
    if active and active != topology_hash:
        return (
            f"active database topology {active} differs from discovered topology {topology_hash}; "
            "normal runs will discard stale state and explicit resume is refused"
        )
    return None


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    return True


def abort_runs(
    db_path: str | Path,
    run_ids: list[str] | None = None,
    terminate: bool = True,
    reason: str = "operator_abort",
) -> dict[str, Any]:
    if run_ids is None:
        rows = active_run_rows(db_path)
    else:
        rows = []
        for run_id in run_ids:
            found = read_only_rows(db_path, "SELECT * FROM runs WHERE run_id=?", (run_id,))
            rows.extend(found)
    live_rows = [row for row in rows if _pid_alive(row.get("pid"))]
    if live_rows and not terminate:
        raise RuntimeError(
            "refusing abort while active runs are alive: "
            + ", ".join(str(row["run_id"]) for row in live_rows)
        )
    if live_rows:
        for row in live_rows:
            try:
                os.kill(int(row["pid"]), signal.SIGTERM)
            except (OSError, ValueError, TypeError):
                pass
        deadline = time.time() + 5
        while time.time() < deadline:
            remaining = [row for row in live_rows if _pid_alive(row.get("pid"))]
            if not remaining:
                break
            time.sleep(0.1)
        still_live = [row for row in live_rows if _pid_alive(row.get("pid"))]
        if still_live:
            raise RuntimeError(
                "failed to terminate active runs: "
                + ", ".join(str(row["run_id"]) for row in still_live)
            )
    if not rows:
        return {"aborted": [], "reason": reason}
    store = SQLiteStore(db_path)
    aborted: list[str] = []
    try:
        for row in rows:
            run_id = row["run_id"]
            store.add_event(run_id, "operator_abort", {"reason": reason})
            store.finish_run(run_id, status="aborted", end_reason=reason)
            aborted.append(run_id)
    finally:
        store.close()
    return {"aborted": aborted, "reason": reason}


def archive_state(
    db_path: str | Path,
    runs_jsonl: str | Path,
    trace_dir: str | Path,
    ip_cache_path: str | Path | None = None,
    reason: str = "topology_change",
) -> dict[str, Any]:
    database = Path(db_path)
    runs_file = Path(runs_jsonl)
    traces = Path(trace_dir)
    cache = Path(ip_cache_path) if ip_cache_path else None
    active = active_run_rows(database)
    live = [row for row in active if _pid_alive(row.get("pid"))]
    if live:
        raise RuntimeError(
            "refusing state archive while active runs are alive: "
            + ", ".join(str(row["run_id"]) for row in live)
        )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_dir = database.parent / "archive" / f"{stamp}-{uuid.uuid4().hex[:6]}"
    archive_dir.mkdir(parents=True, exist_ok=False)
    copied: list[str] = []
    for source in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
        if source.exists():
            target = archive_dir / source.name
            shutil.copy2(source, target)
            copied.append(str(target))
    if runs_file.exists():
        target = archive_dir / "runs.jsonl"
        shutil.copy2(runs_file, target)
        copied.append(str(target))
    if traces.exists():
        trace_archive = archive_dir / "traces"
        trace_archive.mkdir(parents=True, exist_ok=True)
        for source in sorted(traces.glob("*.jsonl")):
            if source.is_file():
                target = trace_archive / source.name
                shutil.copy2(source, target)
                copied.append(str(target))
    if cache and cache.exists():
        target = archive_dir / cache.name
        shutil.copy2(cache, target)
        copied.append(str(target))
    manifest = {
        "reason": reason,
        "created_at": stamp,
        "database": str(database),
        "files": copied,
        "active_runs": [str(row["run_id"]) for row in active],
    }
    manifest_path = archive_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return {"archive": str(archive_dir), "files": copied, "active_runs": manifest["active_runs"]}


def discard_state(
    db_path: str | Path,
    runs_jsonl: str | Path,
    trace_dir: str | Path,
    ip_cache_path: str | Path | None = None,
) -> dict[str, Any]:
    database = Path(db_path)
    runs_file = Path(runs_jsonl)
    traces = Path(trace_dir)
    cache = Path(ip_cache_path) if ip_cache_path else None
    summary: dict[str, Any] = {
        "database": str(database),
        "runs_aborted": [],
        "files_removed": [],
        "runs_jsonl_truncated": False,
        "reinitialized": False,
    }

    active = active_run_rows(database)
    live = [row for row in active if _pid_alive(row.get("pid"))]
    if live:
        raise RuntimeError(
            "refusing state discard while active runs are alive: "
            + ", ".join(str(row["run_id"]) for row in live)
        )

    if active and database.exists():
        store = SQLiteStore(database)
        try:
            for row in active:
                run_id = row["run_id"]
                store.add_event(run_id, "state_discard_abort", {"reason": "state_discard"})
                store.finish_run(run_id, status="aborted", end_reason="state_discard")
                summary["runs_aborted"].append(run_id)
            remaining = active_run_rows(database)
            if remaining:
                raise RuntimeError(f"failed to abort active runs: {remaining}")
        finally:
            store.close()

    summary["archive"] = archive_state(
        database, runs_file, traces, cache, reason="state_discard",
    )

    for suffix in ("", "-wal", "-shm"):
        target = Path(f"{database}{suffix}")
        if target.exists():
            target.unlink()
            summary["files_removed"].append(str(target))

    if runs_file.exists():
        runs_file.write_text("", encoding="utf-8")
        summary["runs_jsonl_truncated"] = True

    if traces.exists():
        for target in sorted(traces.glob("*.jsonl")):
            if target.is_file():
                target.unlink()
                summary["files_removed"].append(str(target))

    if cache and cache.exists():
        cache.unlink()
        summary["files_removed"].append(str(cache))

    SQLiteStore(database).close()
    summary["reinitialized"] = True
    return summary


class SQLiteStore:
    """One-connection, append-first store used only by the harness writer."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.init_db()

    def __enter__(self) -> SQLiteStore:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self.conn
            except Exception:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    @staticmethod
    def _now() -> float:
        return time.time()

    @staticmethod
    def _dumps(value: Any) -> str | None:
        if value is None:
            return None
        return json.dumps(value, sort_keys=True, default=str)

    @staticmethod
    def _loads(value: Any, default: Any = None) -> Any:
        if value is None:
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _db_value(column: str, value: Any) -> Any:
        if column in RUN_BOOL_COLUMNS and value is not None:
            return int(bool(value))
        if column in RUN_JSON_COLUMNS:
            return SQLiteStore._dumps(value)
        return value

    @staticmethod
    def _score_bool(value: Any) -> Any:
        return int(bool(value)) if value is not None else None

    @staticmethod
    def hash_spec(spec: dict[str, Any]) -> str:
        raw = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def git_sha() -> str:
        try:
            return subprocess.check_output(
                ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
            ).strip()
        except Exception:
            return "unknown"

    def init_db(self) -> None:
        """Create tables and additively migrate older databases."""
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    version INTEGER PRIMARY KEY,
                    applied_at REAL NOT NULL,
                    notes TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS scenarios (
                    id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    spec_hash TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (id, version)
                );

                CREATE TABLE IF NOT EXISTS batches (
                    batch_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    command_json TEXT NOT NULL DEFAULT '{}',
                    scenario_ids_json TEXT NOT NULL DEFAULT '[]',
                    backend TEXT NOT NULL DEFAULT '',
                    seed_start INTEGER NOT NULL DEFAULT 0,
                    runs_requested INTEGER NOT NULL DEFAULT 0,
                    reset_cmd TEXT NOT NULL DEFAULT '',
                    config_hash TEXT NOT NULL DEFAULT '',
                    topology_hash TEXT,
                    status TEXT NOT NULL DEFAULT 'running',
                    finished_at REAL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    batch_id TEXT,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    resume_of TEXT,
                    resume_count INTEGER NOT NULL DEFAULT 0,
                    scenario_id TEXT NOT NULL,
                    scenario_version INTEGER NOT NULL DEFAULT 1,
                    spec_hash TEXT,
                    backend TEXT NOT NULL,
                    model TEXT,
                    quant TEXT,
                    temperature REAL,
                    seed INTEGER NOT NULL DEFAULT 0,
                    prompt_hash TEXT,
                    harness_git_sha TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    phase TEXT NOT NULL DEFAULT 'queued',
                    end_reason TEXT,
                    truth TEXT,
                    diagnosis TEXT,
                    diagnosis_raw TEXT,
                    diagnosis_correct INTEGER,
                    diagnosis_attempts INTEGER,
                    diagnosis_first_attempt_correct INTEGER,
                    approval_granted INTEGER,
                    repair TEXT,
                    repair_args_json TEXT,
                    policy_violations_json TEXT,
                    forbidden_reason TEXT,
                    reset_ok INTEGER,
                    baseline_ok INTEGER,
                    inject_ok INTEGER,
                    verify_ok INTEGER,
                    verify_output TEXT,
                    steps INTEGER NOT NULL DEFAULT 0,
                    tokens_used INTEGER NOT NULL DEFAULT 0,
                    started_at REAL,
                    updated_at REAL NOT NULL,
                    finished_at REAL,
                    trace_path TEXT,
                    config_hash TEXT,
                    topology_hash TEXT,
                    interactive INTEGER NOT NULL DEFAULT 0,
                    launch_id TEXT,
                    pid INTEGER,
                    state_json TEXT,
                    state_seq INTEGER NOT NULL DEFAULT 0,
                    seq INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    FOREIGN KEY(batch_id) REFERENCES batches(batch_id)
                );

                CREATE TABLE IF NOT EXISTS messages (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content_json TEXT,
                    tool_calls_json TEXT,
                    tool_call_id TEXT,
                    tokens INTEGER,
                    backend TEXT,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (run_id, seq),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS tool_calls (
                    run_id TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    msg_seq INTEGER,
                    name TEXT NOT NULL,
                    args_json TEXT NOT NULL DEFAULT '{}',
                    mutating INTEGER NOT NULL DEFAULT 0,
                    destructive INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'proposed',
                    approved_by TEXT,
                    result_raw TEXT,
                    result_sent TEXT,
                    truncated INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    started_at REAL,
                    finished_at REAL,
                    PRIMARY KEY (run_id, call_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS llm_requests (
                    run_id TEXT NOT NULL,
                    req_id INTEGER NOT NULL,
                    backend TEXT NOT NULL,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    tokens_in INTEGER NOT NULL DEFAULT 0,
                    tokens_out INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (run_id, req_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS trace_events (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    ts REAL NOT NULL,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, seq),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS run_artifacts (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_type TEXT NOT NULL DEFAULT 'text/plain',
                    sha256 TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (run_id, seq),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS state_snapshots (
                    run_id TEXT NOT NULL,
                    snapshot_seq INTEGER NOT NULL,
                    event_seq INTEGER NOT NULL,
                    ts REAL NOT NULL,
                    source TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, snapshot_seq),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS ui_stream (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    ts REAL NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    PRIMARY KEY (run_id, seq),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS diagnosis_attempts (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    raw TEXT,
                    normalized TEXT,
                    accepted INTEGER NOT NULL DEFAULT 0,
                    reason TEXT,
                    ts REAL NOT NULL,
                    PRIMARY KEY (run_id, seq),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS policy_violations (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    tool TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    ts REAL NOT NULL,
                    PRIMARY KEY (run_id, seq),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS scores (
                    run_id TEXT NOT NULL,
                    scorer_version TEXT NOT NULL,
                    diagnosis REAL,
                    diagnosis_correct INTEGER,
                    diagnosis_attempts INTEGER,
                    diagnosis_first_attempt_correct INTEGER,
                    repair_action TEXT,
                    repair_acceptable INTEGER,
                    repair_forbidden INTEGER,
                    recovered INTEGER,
                    policy_violations INTEGER NOT NULL DEFAULT 0,
                    loop_success INTEGER,
                    strict_loop_success INTEGER,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (run_id, scorer_version),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS score_reports (
                    report_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    filters_json TEXT NOT NULL DEFAULT '{}',
                    format TEXT NOT NULL DEFAULT 'md',
                    content TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_runs_scenario_backend ON runs(scenario_id, backend);
                CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
                CREATE INDEX IF NOT EXISTS idx_runs_resume_of ON runs(resume_of);
                CREATE INDEX IF NOT EXISTS idx_events_run ON trace_events(run_id, seq);
                CREATE INDEX IF NOT EXISTS idx_messages_run ON messages(run_id, seq);
                CREATE INDEX IF NOT EXISTS idx_tool_calls_run_status ON tool_calls(run_id, status);
                CREATE INDEX IF NOT EXISTS idx_artifacts_run_kind ON run_artifacts(run_id, kind, seq);
                CREATE INDEX IF NOT EXISTS idx_llm_requests_run ON llm_requests(run_id, req_id);
                CREATE INDEX IF NOT EXISTS idx_state_snapshots_run ON state_snapshots(run_id, snapshot_seq);
                CREATE INDEX IF NOT EXISTS idx_ui_stream_run ON ui_stream(run_id, seq);
                """
            )
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
            for column, definition in RUN_COLUMNS.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {definition}")
            batch_columns = {row["name"] for row in conn.execute("PRAGMA table_info(batches)")}
            if "topology_hash" not in batch_columns:
                conn.execute("ALTER TABLE batches ADD COLUMN topology_hash TEXT")
            score_columns = {row["name"] for row in conn.execute("PRAGMA table_info(scores)")}
            for column, definition in SCORE_COLUMNS.items():
                if column not in score_columns:
                    conn.execute(f"ALTER TABLE scores ADD COLUMN {column} {definition}")
            conn.execute(
                "UPDATE batches SET backend='ollama' WHERE backend=?",
                (LEGACY_BACKEND_TOKENS[0],),
            )
            conn.execute(
                "UPDATE runs SET backend='ollama' WHERE backend=?",
                (LEGACY_BACKEND_TOKENS[0],),
            )
            conn.execute(
                "UPDATE messages SET backend='ollama' WHERE backend=?",
                (LEGACY_BACKEND_TOKENS[0],),
            )
            conn.execute(
                "UPDATE llm_requests SET backend='ollama' WHERE backend=?",
                (LEGACY_BACKEND_TOKENS[0],),
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(version, applied_at, notes) VALUES (?, ?, ?)",
                (SCHEMA_VERSION, self._now(), "event-log-first schema; topology, snapshots, UI stream, fault presence, strict scoring"),
            )
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def migrate(self) -> dict[str, Any]:
        self.init_db()
        return {"schema_version": SCHEMA_VERSION}

    def ensure_scenario(self, scenario_id: str, spec_hash: str, spec_json: str) -> int:
        """Pin one immutable scenario version by spec hash."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT version, spec_hash FROM scenarios WHERE id=? ORDER BY version", (scenario_id,)
            ).fetchall()
            for row in rows:
                if row["spec_hash"] == spec_hash:
                    return int(row["version"])
            version = int(rows[-1]["version"]) + 1 if rows else 1
            conn.execute(
                """INSERT INTO scenarios(id, version, spec_hash, spec_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (scenario_id, version, spec_hash, spec_json, self._now()),
            )
            return version

    def create_batch(
        self,
        batch_id: str | None = None,
        command: list[str] | None = None,
        scenario_ids: list[str] | None = None,
        backend: str = "",
        seed_start: int = 0,
        runs_requested: int = 0,
        reset_cmd: str = "",
        config_hash: str = "",
        topology_hash: str | None = None,
    ) -> str:
        batch_id = batch_id or f"batch-{uuid.uuid4().hex[:12]}"
        with self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO batches(
                       batch_id, created_at, command_json, scenario_ids_json, backend,
                       seed_start, runs_requested, reset_cmd, config_hash, topology_hash, status
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')""",
                (
                    batch_id,
                    self._now(),
                    self._dumps(command or []),
                    self._dumps(scenario_ids or []),
                    backend,
                    seed_start,
                    runs_requested,
                    reset_cmd,
                    config_hash,
                    topology_hash,
                ),
            )
        return batch_id

    def update_batch(self, batch_id: str, status: str, finished_at: float | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE batches SET status=?, finished_at=? WHERE batch_id=?",
                (status, finished_at or self._now(), batch_id),
            )

    def run_exists(self, run_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return row is not None

    def create_run(self, run_id: str, **fields: Any) -> dict[str, Any]:
        payload = {
            "run_id": run_id,
            "batch_id": fields.get("batch_id"),
            "attempt": fields.get("attempt", 1),
            "resume_of": fields.get("resume_of"),
            "resume_count": fields.get("resume_count", 0),
            "scenario_id": fields.get("scenario_id") or fields.get("scenario") or "unknown",
            "scenario_version": fields.get("scenario_version", 1),
            "spec_hash": fields.get("spec_hash") or fields.get("config_hash"),
            "backend": fields.get("backend", "unknown"),
            "model": fields.get("model"),
            "quant": fields.get("quant"),
            "temperature": fields.get("temperature", 0.2),
            "seed": fields.get("seed", 0),
            "prompt_hash": fields.get("prompt_hash"),
            "harness_git_sha": fields.get("harness_git_sha") or self.git_sha(),
            "status": fields.get("status", "pending"),
            "phase": fields.get("phase", "queued"),
            "end_reason": fields.get("end_reason"),
            "truth": fields.get("truth"),
            "started_at": fields.get("started_at", self._now()),
            "updated_at": self._now(),
            "config_hash": fields.get("config_hash"),
            "topology_hash": fields.get("topology_hash"),
            "interactive": fields.get("interactive", False),
            "launch_id": fields.get("launch_id"),
            "pid": fields.get("pid"),
        }
        columns = list(payload)
        values = [self._db_value(column, payload[column]) for column in columns]
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO runs({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                values,
            )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown run_id: {run_id}")
        return dict(row)

    def update_run(self, run_id: str, **fields: Any) -> None:
        allowed = [name for name in fields if name in RUN_COLUMNS and name != "run_id"]
        if not allowed:
            return
        assignments = [f"{name}=?" for name in allowed]
        values = [self._db_value(name, fields[name]) for name in allowed]
        values.append(self._now())
        values.append(run_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE runs SET {', '.join(assignments)}, updated_at=? WHERE run_id=?",
                values,
            )

    def append_message(
        self,
        run_id: str,
        role: str,
        content: Any,
        tool_calls: Any = None,
        tool_call_id: str | None = None,
        tokens: int | None = None,
        backend: str | None = None,
    ) -> int:
        with self._connect() as conn:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM messages WHERE run_id=?", (run_id,)
            ).fetchone()["seq"]
            conn.execute(
                """INSERT INTO messages(run_id, seq, role, content_json, tool_calls_json,
                                         tool_call_id, tokens, backend, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    seq,
                    role,
                    self._dumps(content),
                    self._dumps(tool_calls),
                    tool_call_id,
                    tokens,
                    backend,
                    self._now(),
                ),
            )
        return int(seq)

    def save_messages(self, run_id: str, messages: list[dict[str, Any]], backend: str | None = None) -> None:
        """Append messages that are not already present; the log is never rewritten."""
        existing = len(self.load_messages(run_id))
        for message in messages[existing:]:
            self.append_message(
                run_id,
                str(message.get("role", "unknown")),
                message.get("content"),
                message.get("tool_calls"),
                message.get("tool_call_id"),
                backend=backend,
            )

    def load_messages(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE run_id=? ORDER BY seq", (run_id,)
            ).fetchall()
        messages = []
        for row in rows:
            message = {"role": row["role"], "content": self._loads(row["content_json"], default="")}
            if row["tool_calls_json"] is not None:
                message["tool_calls"] = self._loads(row["tool_calls_json"], default=[])
            if row["tool_call_id"] is not None:
                message["tool_call_id"] = row["tool_call_id"]
            if row["backend"] is not None:
                message["backend"] = row["backend"]
            if row["tokens"] is not None:
                message["tokens"] = row["tokens"]
            messages.append(message)
        return messages

    def add_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> int:
        ts = float(payload.get("ts") or self._now())
        with self._connect() as conn:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM trace_events WHERE run_id=?",
                (run_id,),
            ).fetchone()["seq"]
            conn.execute(
                "INSERT INTO trace_events(run_id, seq, ts, type, payload_json) VALUES (?, ?, ?, ?, ?)",
                (run_id, seq, ts, event_type, self._dumps(payload)),
            )
            conn.execute(
                "UPDATE runs SET seq=?, updated_at=? WHERE run_id=?", (seq, self._now(), run_id)
            )
        return int(seq)

    def load_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trace_events WHERE run_id=? ORDER BY seq", (run_id,)
            ).fetchall()
        events = []
        for row in rows:
            payload = self._loads(row["payload_json"], default={}) or {}
            event = dict(payload)
            event.setdefault("ts", row["ts"])
            event.setdefault("type", row["type"])
            events.append(event)
        return events

    def begin_tool_call(
        self,
        run_id: str,
        call_id: str,
        msg_seq: int | None,
        name: str,
        args: dict[str, Any],
        mutating: bool = False,
        destructive: bool = False,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO tool_calls(
                       run_id, call_id, msg_seq, name, args_json, mutating, destructive,
                       status, started_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?)""",
                (
                    run_id,
                    call_id,
                    msg_seq,
                    name,
                    self._dumps(args) or "{}",
                    int(bool(mutating)),
                    int(bool(destructive)),
                    self._now(),
                ),
            )

    def update_tool_call_status(self, run_id: str, call_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tool_calls SET status=? WHERE run_id=? AND call_id=?",
                (status, run_id, call_id),
            )

    def finish_tool_call(
        self,
        run_id: str,
        call_id: str,
        status: str,
        result_raw: str,
        result_sent: str,
        truncated: bool,
        approved_by: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE tool_calls
                   SET status=?, result_raw=?, result_sent=?, truncated=?, approved_by=?,
                       error=?, finished_at=?
                   WHERE run_id=? AND call_id=?""",
                (
                    status,
                    result_raw,
                    result_sent,
                    int(bool(truncated)),
                    approved_by,
                    error,
                    self._now(),
                    run_id,
                    call_id,
                ),
            )

    def load_tool_calls(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tool_calls WHERE run_id=? ORDER BY started_at, call_id", (run_id,)
            ).fetchall()
        calls = []
        for row in rows:
            call = dict(row)
            call["args"] = self._loads(call.pop("args_json"), default={})
            calls.append(call)
        return calls

    def unfinished_tool_calls(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM tool_calls
                   WHERE run_id=? AND status IN ('proposed', 'started', 'unknown')
                   ORDER BY started_at, call_id""",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def has_unknown_mutating_tool_call(self, run_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT 1 FROM tool_calls
                    WHERE run_id=? AND mutating=1 AND status IN ('proposed', 'started', 'unknown')

                   LIMIT 1""",
                (run_id,),
            ).fetchone()
        return row is not None

    def add_llm_request(
        self,
        run_id: str,
        backend: str,
        latency_ms: int,
        tokens_in: int,
        tokens_out: int,
        error: str | None = None,
        attempt: int = 1,
    ) -> int:
        with self._connect() as conn:
            req_id = conn.execute(
                "SELECT COALESCE(MAX(req_id), 0) + 1 AS req_id FROM llm_requests WHERE run_id=?",
                (run_id,),
            ).fetchone()["req_id"]
            conn.execute(
                """INSERT INTO llm_requests(run_id, req_id, backend, latency_ms, tokens_in,
                                             tokens_out, error, attempt, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, req_id, backend, latency_ms, tokens_in, tokens_out, error, attempt, self._now()),
            )
        return int(req_id)

    def token_totals(self, run_id: str) -> tuple[int, int]:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COALESCE(SUM(tokens_in), 0) AS tin, COALESCE(SUM(tokens_out), 0) AS tout
                   FROM llm_requests WHERE run_id=?""",
                (run_id,),
            ).fetchone()
        return int(row["tin"]), int(row["tout"])

    def add_artifact(
        self,
        run_id: str,
        kind: str,
        content: str | bytes,
        content_type: str = "text/plain",
    ) -> int:
        if isinstance(content, bytes):
            text = content.decode("utf-8", errors="replace")
        else:
            text = str(content)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._connect() as conn:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM run_artifacts WHERE run_id=?",
                (run_id,),
            ).fetchone()["seq"]
            conn.execute(
                """INSERT INTO run_artifacts(run_id, seq, kind, content, content_type, sha256, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (run_id, seq, kind, text, content_type, digest, self._now()),
            )
        return int(seq)

    def get_latest_artifact(self, run_id: str, kind: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM run_artifacts WHERE run_id=? AND kind=?
                   ORDER BY seq DESC LIMIT 1""",
                (run_id, kind),
            ).fetchone()
        return dict(row) if row else None

    def load_artifacts(self, run_id: str, kind: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM run_artifacts WHERE run_id=?"
        params: list[Any] = [run_id]
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY seq"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def add_diagnosis_attempt(
        self, run_id: str, raw: Any, normalized: Any, accepted: bool, reason: str | None = None
    ) -> None:
        with self._connect() as conn:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM diagnosis_attempts WHERE run_id=?",
                (run_id,),
            ).fetchone()["seq"]
            conn.execute(
                """INSERT INTO diagnosis_attempts(run_id, seq, raw, normalized, accepted, reason, ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    seq,
                    None if raw is None else str(raw),
                    None if normalized is None else str(normalized),
                    int(bool(accepted)),
                    reason,
                    self._now(),
                ),
            )

    def add_policy_violation(self, run_id: str, tool: str, reason: str) -> None:
        with self._connect() as conn:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM policy_violations WHERE run_id=?",
                (run_id,),
            ).fetchone()["seq"]
            conn.execute(
                "INSERT INTO policy_violations(run_id, seq, tool, reason, ts) VALUES (?, ?, ?, ?, ?)",
                (run_id, seq, tool, reason, self._now()),
            )

    def start_lease(self, run_id: str, owner: str, ttl_seconds: int = 300) -> bool:
        now = self._now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT lease_owner, lease_expires_at FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return False
            if row["lease_expires_at"] and row["lease_expires_at"] > now and row["lease_owner"] != owner:
                return False
            conn.execute(
                "UPDATE runs SET lease_owner=?, lease_expires_at=? WHERE run_id=?",
                (owner, now + ttl_seconds, run_id),
            )
        return True

    def renew_lease(self, run_id: str, ttl_seconds: int = 300) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                """UPDATE runs SET lease_expires_at=?, updated_at=?
                   WHERE run_id=? AND lease_owner IS NOT NULL""",
                (now + ttl_seconds, now, run_id),
            )

    def release_lease(self, run_id: str) -> None:
        self.update_run(run_id, lease_owner=None, lease_expires_at=None)

    def resume_candidates(self, scenario_id: str | None = None, backend: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM runs WHERE status IN ('running', 'interrupted') ORDER BY updated_at DESC"
        clauses: list[str] = []
        params: list[Any] = []
        if scenario_id:
            clauses.append("scenario_id=?")
            params.append(scenario_id)
        if backend:
            clauses.append("backend=?")
            params.append(backend)
        if clauses:
            sql = sql.replace("ORDER BY", "AND " + " AND ".join(clauses) + " ORDER BY")
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def rebuild_state(self, run_id: str) -> dict[str, Any]:
        """Derive agent resume state from append-only logs."""
        row = self.get_run(run_id)
        events = self.load_events(run_id)
        calls = self.load_tool_calls(run_id)
        tokens_in, tokens_out = self.token_totals(run_id)
        state: dict[str, Any] = {
            "diagnosis": None,
            "diagnosis_raw": None,
            "diagnosis_submitted": False,
            "diagnosis_correct": False,
            "approval_token": None,
            "diagnosis_attempts": 0,
            "diagnosis_first_attempt_correct": False,
            "repair": None,
            "repair_args": None,
            "policy_violations": [],
            "forbidden_reason": None,
            "steps": 0,
            "tokens_used": tokens_in + tokens_out,
            "diagnosis_nudged": False,
            "trace_path": row.get("trace_path") or f"traces/{run_id}.jsonl",
            "seq": int(row.get("seq") or 0),
        }
        for event in events:
            kind = event.get("type")
            if kind == "assistant":
                state["steps"] = int(state["steps"]) + 1
            elif kind == "approval":
                state["diagnosis_submitted"] = True
                state["diagnosis_attempts"] = int(event.get("diagnosis_attempts") or 0)
                state["diagnosis"] = event.get("diagnosis")
                state["diagnosis_raw"] = event.get("raw")
                state["diagnosis_correct"] = False
                state["approval_token"] = None
                if event.get("granted"):
                    state["diagnosis_correct"] = True
                    state["diagnosis_first_attempt_correct"] = bool(event.get("diagnosis_first_attempt_correct"))
                    state["approval_token"] = event.get("approval_token")
            elif kind == "nudge":
                state["diagnosis_nudged"] = True
            elif kind == "tool" and event.get("name") in REPAIR_TOOLS:
                text = str(event.get("result") or "")
                if not text.startswith("BLOCKED:") and not text.startswith("ERROR:"):
                    args = dict(event.get("args") or {})
                    args.pop("approval_token", None)
                    state["repair"] = normalize_repair_name(event.get("name"))
                    state["repair_args"] = args
        with self._connect() as conn:
            state["policy_violations"] = [
                f"{row['tool']}: {row['reason']}"
                for row in conn.execute(
                    "SELECT tool, reason FROM policy_violations WHERE run_id=? ORDER BY seq", (run_id,)
                )
            ]
            for call in calls:
                if call["name"] in REPAIR_TOOLS and call["status"] == "denied" and call.get("error"):
                    state["forbidden_reason"] = call["error"]
        return state

    def get_state(self, run_id: str) -> dict[str, Any]:
        row = self.get_run(run_id)
        cached = self._loads(row.get("state_json"), default={}) or {}
        if cached and cached.get("seq") == row.get("state_seq") == row.get("seq"):
            cached.setdefault("trace_path", row.get("trace_path") or f"traces/{run_id}.jsonl")
            rebuilt = self.rebuild_state(run_id)
            for key in (
                "approval_token", "diagnosis_correct", "diagnosis_attempts",
                "diagnosis_first_attempt_correct",
            ):
                cached[key] = rebuilt.get(key)
            return cached
        rebuilt = self.rebuild_state(run_id)
        self.set_state(run_id, rebuilt, source="rebuild")
        return rebuilt

    def set_state(self, run_id: str, state: dict[str, Any], source: str = "checkpoint") -> None:
        cache = dict(state)
        cache.pop("messages", None)
        cache.pop("approval_token", None)
        seq = int(cache.get("seq") or 0)
        with self._connect() as conn:
            if not seq:
                row = conn.execute("SELECT seq FROM runs WHERE run_id=?", (run_id,)).fetchone()
                seq = int((row["seq"] if row else 0) or 0)
            cache["seq"] = seq
            now = self._now()
            payload = self._dumps(cache)
            conn.execute(
                "UPDATE runs SET state_json=?, state_seq=?, updated_at=? WHERE run_id=?",
                (payload, seq, now, run_id),
            )
            snapshot_seq = conn.execute(
                "SELECT COALESCE(MAX(snapshot_seq), 0) + 1 AS seq FROM state_snapshots WHERE run_id=?",
                (run_id,),
            ).fetchone()["seq"]
            conn.execute(
                """INSERT INTO state_snapshots(run_id, snapshot_seq, event_seq, ts, source, state_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (run_id, snapshot_seq, seq, now, source, payload),
            )

    def load_state_snapshots(self, run_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM state_snapshots WHERE run_id=? ORDER BY snapshot_seq"
        params: list[Any] = [run_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        snapshots = []
        for row in rows:
            state = self._loads(row["state_json"], default={}) or {}
            snapshot = dict(state)
            snapshot.update({
                "snapshot_seq": int(row["snapshot_seq"]),
                "event_seq": int(row["event_seq"]),
                "ts": float(row["ts"]),
                "source": row["source"],
            })
            snapshots.append(snapshot)
        return snapshots

    def append_ui_chunk(self, run_id: str, kind: str, text: str) -> int:
        with self._connect() as conn:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM ui_stream WHERE run_id=?",
                (run_id,),
            ).fetchone()["seq"]
            conn.execute(
                "INSERT INTO ui_stream(run_id, seq, ts, kind, text) VALUES (?, ?, ?, ?, ?)",
                (run_id, seq, self._now(), kind, text),
            )
        return int(seq)

    def load_ui_stream(self, run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ui_stream WHERE run_id=? AND seq>? ORDER BY seq",
                (run_id, after_seq),
            ).fetchall()
        return [{
            "seq": int(row["seq"]),
            "ts": float(row["ts"]),
            "kind": row["kind"],
            "text": row["text"],
        } for row in rows]

    def record_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        record: dict[str, Any] = {}
        for key, column in RECORD_TO_COLUMN.items():
            value = row.get(column)
            if column in RUN_BOOL_COLUMNS:
                record[key] = bool(value) if value is not None else None
            elif column in RUN_JSON_COLUMNS:
                default = [] if column == "policy_violations_json" else None
                record[key] = self._loads(value, default=default)
            else:
                record[key] = value
        record["policy_violations"] = record.get("policy_violations") or []
        for key in ("resume_count", "spec_hash", "scenario_version", "end_reason", "model", "backend", "topology_hash", "launch_id", "pid"):
            if row.get(key) is not None:
                record[key] = row[key]
        if row.get("interactive") is not None:
            record["interactive"] = bool(row["interactive"])
        record["backend"] = normalize_backend(record.get("backend"))
        return record

    @staticmethod
    def _score_rank(score: dict[str, Any]) -> tuple[int, float]:
        preferred = 1 if score.get("scorer_version") == SCORER_VERSION else 0
        return preferred, float(score.get("created_at") or 0)

    def finish_run(
        self,
        run_id: str,
        record: dict[str, Any] | None = None,
        status: str = "finished",
        end_reason: str | None = None,
        state: dict[str, Any] | None = None,
        score: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "status": status,
            "phase": status,
            "end_reason": end_reason,
            "finished_at": self._now(),
        }
        if record:
            skipped = {"run_id"}
            if end_reason is not None:
                skipped.add("end_reason")
            for key, column in RECORD_TO_COLUMN.items():
                if key in record and key not in skipped:
                    fields[column] = record[key]
            self.add_artifact(run_id, "final_record", self._dumps(record) or "null", "application/json")
        self.update_run(run_id, **fields)
        if state is not None:
            self.set_state(run_id, state)
        if score is not None:
            payload = {
                "run_id": run_id,
                "scorer_version": score.get("scorer_version", "legacy-record-v1"),
                "diagnosis": score.get("diagnosis"),
                "diagnosis_correct": score.get("diagnosis_correct"),
                "diagnosis_attempts": score.get("diagnosis_attempts"),
                "diagnosis_first_attempt_correct": score.get("diagnosis_first_attempt_correct"),
                "repair_action": score.get("repair_action"),
                "repair_acceptable": score.get("repair_acceptable"),
                "repair_forbidden": score.get("repair_forbidden"),
                "recovered": score.get("recovered"),
                "policy_violations": score.get("policy_violations", 0),
                "loop_success": score.get("loop_success"),
                "strict_loop_success": score.get("strict_loop_success"),
                "created_at": self._now(),
            }
            with self._connect() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO scores(
                           run_id, scorer_version, diagnosis, diagnosis_correct, diagnosis_attempts,
                           diagnosis_first_attempt_correct, repair_action, repair_acceptable,
                           repair_forbidden, recovered, policy_violations, loop_success,
                           strict_loop_success, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        payload["run_id"],
                        payload["scorer_version"],
                        payload["diagnosis"],
                        self._db_value("diagnosis_correct", payload["diagnosis_correct"]),
                        payload["diagnosis_attempts"],
                        self._score_bool(payload["diagnosis_first_attempt_correct"]),
                        payload["repair_action"],
                        self._db_value("repair_acceptable", payload["repair_acceptable"]),
                        self._db_value("repair_forbidden", payload["repair_forbidden"]),
                        self._db_value("recovered", payload["recovered"]),
                        payload["policy_violations"],
                        self._db_value("loop_success", payload["loop_success"]),
                        self._score_bool(payload["strict_loop_success"]),
                        payload["created_at"],
                    ),
                )
        self.release_lease(run_id)
        return self.get_run(run_id)

    def load_records(
        self,
        include_resumed: bool = False,
        include_aborted: bool = False,
        include_interactive: bool = False,
        include_provider_errors: bool = False,
        include_unverified_faults: bool = False,
    ) -> list[dict[str, Any]]:
        clauses = ["status='finished'"]
        if not include_resumed:
            clauses.append("resume_count=0")
        if not include_aborted:
            clauses.append("(end_reason IS NULL OR end_reason != 'legacy_exploratory')")
        if not include_interactive:
            clauses.append("interactive=0")
        if not include_provider_errors:
            clauses.append("(end_reason IS NULL OR (end_reason NOT LIKE 'provider_error%' AND end_reason NOT IN ('error', 'fault_not_observed', 'injection_failed')))")
        if not include_unverified_faults:
            clauses.append("fault_present=1")
        sql = "SELECT * FROM runs WHERE " + " AND ".join(clauses)
        sql += " ORDER BY COALESCE(started_at, 0), run_id"
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
            run_ids = [row["run_id"] for row in rows]
            score_rows: list[sqlite3.Row] = []
            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                score_rows = conn.execute(
                    f"SELECT * FROM scores WHERE run_id IN ({placeholders})", run_ids
                ).fetchall()
        score_by_run: dict[str, dict[str, Any]] = {}
        for row in score_rows:
            score = dict(row)
            run_id = str(score["run_id"])
            current = score_by_run.get(run_id)
            if current is None or self._score_rank(score) > self._score_rank(current):
                score_by_run[run_id] = score
        records: list[dict[str, Any]] = []
        for row in rows:
            if row["truth"] is None:
                continue
            record = self.record_from_row(dict(row))
            score = score_by_run.get(str(row["run_id"]), {})
            score_keys = (
                "diagnosis_attempts", "diagnosis_first_attempt_correct", "repair_acceptable",
                "repair_forbidden", "strict_loop_success", "loop_success", "scorer_version",
            )
            for key in score_keys:
                if key in score and score[key] is not None:
                    record[key] = bool(score[key]) if key in SCORE_BOOL_FIELDS else score[key]
            records.append(record)
        return records

    def export_trace(self, run_id: str, trace_dir: str | Path) -> Path:
        directory = Path(trace_dir)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{run_id}.jsonl"
        events = self.load_events(run_id)
        target.write_text(
            "".join(
                json.dumps(Message.redact_payload(event), sort_keys=True, default=str) + "\n"
                for event in events
            ),
            encoding="utf-8",
        )
        return target

    def export_all(self, runs_path: str | Path, trace_dir: str | Path) -> dict[str, Any]:
        runs_path = Path(runs_path)
        trace_dir = Path(trace_dir)
        runs_path.parent.mkdir(parents=True, exist_ok=True)
        trace_dir.mkdir(parents=True, exist_ok=True)
        records = self.load_records(
            include_resumed=True,
            include_interactive=True,
            include_provider_errors=True,
            include_unverified_faults=True,
        )
        runs_path.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )
        with self._connect() as conn:
            run_ids = [row["run_id"] for row in conn.execute("SELECT run_id FROM runs")]
        trace_count = 0
        snapshot_count = 0
        stream_count = 0
        for run_id in run_ids:
            events = self.load_events(run_id)
            if events:
                self.export_trace(run_id, trace_dir)
                trace_count += 1
            snapshots = self.load_state_snapshots(run_id)
            if snapshots:
                target = trace_dir / f"{run_id}.snapshots.jsonl"
                target.write_text(
                    "".join(json.dumps(snapshot, sort_keys=True) + "\n" for snapshot in snapshots),
                    encoding="utf-8",
                )
                snapshot_count += len(snapshots)
            chunks = self.load_ui_stream(run_id)
            if chunks:
                target = trace_dir / f"{run_id}.ui.jsonl"
                target.write_text(
                    "".join(json.dumps(chunk, sort_keys=True) + "\n" for chunk in chunks),
                    encoding="utf-8",
                )
                stream_count += len(chunks)
        return {
            "runs": len(records),
            "traces": trace_count,
            "snapshots": snapshot_count,
            "stream_chunks": stream_count,
        }

    def import_legacy(
        self, runs_path: str | Path = "runs.jsonl", trace_dir: str | Path = "traces"
    ) -> dict[str, Any]:
        runs_path = Path(runs_path)
        trace_dir = Path(trace_dir)
        if not runs_path.exists():
            return {"imported": 0, "skipped": 0, "trace_events": 0, "batch_id": None}

        batch_id = f"legacy-import-{uuid.uuid4().hex[:8]}"
        batch_created = False
        imported = 0
        skipped = 0
        trace_events = 0

        for raw_line in runs_path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except ValueError:
                skipped += 1
                continue
            run_id = record.get("run_id")
            if not run_id or self.run_exists(str(run_id)):
                skipped += 1
                continue
            if not batch_created:
                self.create_batch(
                    batch_id=batch_id,
                    command=["db_cli.py", "import-legacy"],
                    scenario_ids=[],
                    backend="legacy",
                    config_hash="legacy",
                )
                batch_created = True

            exploratory = "approval_granted" not in record
            scenario_id = str(record.get("scenario", "unknown"))
            version = self.ensure_scenario(scenario_id, "legacy", "{}")
            events = []
            trace_path = trace_dir / f"{run_id}.jsonl"
            if trace_path.exists():
                for line in trace_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        continue
            timestamps = [float(event["ts"]) for event in events if isinstance(event.get("ts"), (int, float))]
            backend = normalize_backend(record.get("backend", "unknown"))

            self.create_run(
                str(run_id),
                batch_id=batch_id,
                scenario_id=scenario_id,
                scenario_version=version,
                spec_hash="legacy",
                backend=backend,
                seed=0,
                status="aborted" if exploratory else "finished",
                phase="legacy_import",
                end_reason="legacy_exploratory" if exploratory else "imported",
                truth=record.get("truth"),
                started_at=min(timestamps) if timestamps else self._now(),
                config_hash="legacy",
            )
            terminal_fields = {
                key: value for key, value in record.items() if key in RECORD_TO_COLUMN and key != "run_id"
            }
            terminal_fields.update(
                {
                    "backend": backend,
                    "status": "aborted" if exploratory else "finished",
                    "phase": "legacy_import",
                    "end_reason": "legacy_exploratory" if exploratory else "imported",
                    "finished_at": max(timestamps) if timestamps else self._now(),
                }
            )
            self.update_run(str(run_id), **terminal_fields)
            self.add_artifact(
                str(run_id), "final_record", json.dumps(record, sort_keys=True), "application/json"
            )
            for event in events:
                self.add_event(str(run_id), str(event.get("type", "unknown")), event)
                trace_events += 1
            imported += 1

        if batch_created:
            self.update_batch(batch_id, "complete")
        return {
            "imported": imported,
            "skipped": skipped,
            "trace_events": trace_events,
            "batch_id": batch_id if batch_created else None,
        }

    def doctor(self) -> dict[str, Any]:
        self.init_db()
        tables = [
            "scenarios", "batches", "runs", "messages", "tool_calls", "llm_requests",
            "trace_events", "run_artifacts", "state_snapshots", "ui_stream",
            "diagnosis_attempts", "policy_violations", "scores", "score_reports",
        ]
        counts: dict[str, int] = {}
        with self._connect() as conn:
            for table in tables:
                counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            version = int(conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0] or 0)
            missing_final = [
                row["run_id"]
                for row in conn.execute(
                    """SELECT r.run_id FROM runs r
                       WHERE r.status='finished'
                       AND NOT EXISTS (
                           SELECT 1 FROM run_artifacts a
                           WHERE a.run_id=r.run_id AND a.kind='final_record'
                       )"""
                )
            ]
            unknown_calls = [
                row["run_id"]
                for row in conn.execute(
                    "SELECT DISTINCT run_id FROM tool_calls WHERE status IN ('proposed', 'started', 'unknown')"
                )
            ]
            seq_mismatch = [
                row["run_id"]
                for row in conn.execute(
                    """SELECT r.run_id FROM runs r
                       WHERE r.seq != COALESCE((SELECT MAX(seq) FROM trace_events e WHERE e.run_id=r.run_id), 0)"""
                )
            ]
        issues = []
        if version != SCHEMA_VERSION:
            issues.append(f"schema version {version} != {SCHEMA_VERSION}")
        if missing_final:
            issues.append(f"{len(missing_final)} finished runs missing final_record artifact")
        if unknown_calls:
            issues.append(f"{len(unknown_calls)} runs have in-flight tool calls")
        if seq_mismatch:
            issues.append(f"{len(seq_mismatch)} runs have stale checkpoint seq markers")
        return {
            "database": str(self.path),
            "schema_version": version,
            "counts": counts,
            "finished_runs_missing_final_record": missing_final,
            "runs_with_in_flight_tool_calls": unknown_calls,
            "runs_with_stale_seq": seq_mismatch,
            "issues": issues,
            "ok": not issues,
        }
