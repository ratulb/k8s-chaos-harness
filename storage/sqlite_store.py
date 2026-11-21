"""SQLite-backed authoritative store for benchmark runs.

The event tables are the source of truth: messages, tool_calls, llm_requests
and trace_events are append-only. A runs.state_json blob is only a cache that
is rebuilt from the log whenever its seq marker is stale.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

DIAGNOSIS_TOOL = "submit_diagnosis"
REPAIR_TOOLS = {
    "restart_member",
    "member_remove_readd",
    "kube_etcd_switch_rebuild",
    "snapshot_restore_full",
    "reset_cluster",
}
DESTRUCTIVE_TOOLS = {"snapshot_restore_full", "reset_cluster", "member_remove_readd", "kube_etcd_switch_rebuild"}

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
    "approval_granted": "INTEGER",
    "repair": "TEXT",
    "repair_args_json": "TEXT",
    "policy_violations_json": "TEXT",
    "forbidden_reason": "TEXT",
    "reset_ok": "INTEGER",
    "baseline_ok": "INTEGER",
    "inject_ok": "INTEGER",
    "verify_ok": "INTEGER",
    "steps": "INTEGER NOT NULL DEFAULT 0",
    "tokens_used": "INTEGER NOT NULL DEFAULT 0",
    "started_at": "REAL",
    "updated_at": "REAL NOT NULL",
    "finished_at": "REAL",
    "trace_path": "TEXT",
    "config_hash": "TEXT",
    "state_json": "TEXT",
    "state_seq": "INTEGER NOT NULL DEFAULT 0",
    "seq": "INTEGER NOT NULL DEFAULT 0",
    "lease_owner": "TEXT",
    "lease_expires_at": "REAL",
}

RUN_JSON_COLUMNS = {"repair_args_json", "policy_violations_json", "state_json"}
RUN_BOOL_COLUMNS = {
    "diagnosis_correct",
    "approval_granted",
    "reset_ok",
    "baseline_ok",
    "inject_ok",
    "verify_ok",
}

RECORD_TO_COLUMN = {
    "run_id": "run_id",
    "scenario": "scenario_id",
    "backend": "backend",
    "truth": "truth",
    "diagnosis": "diagnosis",
    "diagnosis_raw": "diagnosis_raw",
    "diagnosis_correct": "diagnosis_correct",
    "approval_granted": "approval_granted",
    "repair": "repair",
    "repair_args": "repair_args_json",
    "policy_violations": "policy_violations_json",
    "forbidden_reason": "forbidden_reason",
    "reset_ok": "reset_ok",
    "baseline_ok": "baseline_ok",
    "inject_ok": "inject_ok",
    "verify_ok": "verify_ok",
    "steps": "steps",
    "tokens_used": "tokens_used",
    "trace": "trace_path",
}


class SQLiteStore:
    """One-connection, append-first store used only by the harness writer."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
        self.conn.close()

    def _connect(self) -> sqlite3.Connection:
        return self.conn

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
                    approval_granted INTEGER,
                    repair TEXT,
                    repair_args_json TEXT,
                    policy_violations_json TEXT,
                    forbidden_reason TEXT,
                    reset_ok INTEGER,
                    baseline_ok INTEGER,
                    inject_ok INTEGER,
                    verify_ok INTEGER,
                    steps INTEGER NOT NULL DEFAULT 0,
                    tokens_used INTEGER NOT NULL DEFAULT 0,
                    started_at REAL,
                    updated_at REAL NOT NULL,
                    finished_at REAL,
                    trace_path TEXT,
                    config_hash TEXT,
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
                    repair_action TEXT,
                    repair_acceptable INTEGER,
                    repair_forbidden INTEGER,
                    recovered INTEGER,
                    policy_violations INTEGER NOT NULL DEFAULT 0,
                    loop_success INTEGER,
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
                """
            )
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
            for column, definition in RUN_COLUMNS.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {definition}")
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(version, applied_at, notes) VALUES (?, ?, ?)",
                (SCHEMA_VERSION, self._now(), "event-log-first schema"),
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
    ) -> str:
        batch_id = batch_id or f"batch-{uuid.uuid4().hex[:12]}"
        with self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO batches(
                       batch_id, created_at, command_json, scenario_ids_json, backend,
                       seed_start, runs_requested, reset_cmd, config_hash, status
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')""",
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
            "approval_token": None,
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
                state["diagnosis"] = event.get("diagnosis")
                state["diagnosis_raw"] = event.get("raw")
                if event.get("granted"):
                    state["approval_token"] = event.get("approval_token")
            elif kind == "nudge":
                state["diagnosis_nudged"] = True
            elif kind == "tool" and event.get("name") in REPAIR_TOOLS:
                text = str(event.get("result") or "")
                if not text.startswith("BLOCKED:") and not text.startswith("ERROR:"):
                    args = dict(event.get("args") or {})
                    args.pop("approval_token", None)
                    state["repair"] = event.get("name")
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
            return cached
        rebuilt = self.rebuild_state(run_id)
        self.set_state(run_id, rebuilt)
        return rebuilt

    def set_state(self, run_id: str, state: dict[str, Any]) -> None:
        cache = dict(state)
        cache.pop("messages", None)
        seq = int(cache.get("seq") or self.get_run(run_id).get("seq") or 0)
        cache["seq"] = seq
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET state_json=?, state_seq=?, updated_at=? WHERE run_id=?",
                (self._dumps(cache), seq, self._now(), run_id),
            )

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
        for key in ("resume_count", "spec_hash", "scenario_version", "end_reason", "model", "backend"):
            if row.get(key) is not None:
                record[key] = row[key]
        return record

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
            for key, column in RECORD_TO_COLUMN.items():
                if key in record and key != "run_id":
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
                "repair_action": score.get("repair_action"),
                "repair_acceptable": score.get("repair_acceptable"),
                "repair_forbidden": score.get("repair_forbidden"),
                "recovered": score.get("recovered"),
                "policy_violations": score.get("policy_violations", 0),
                "loop_success": score.get("loop_success"),
                "created_at": self._now(),
            }
            with self._connect() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO scores(
                           run_id, scorer_version, diagnosis, diagnosis_correct, repair_action,
                           repair_acceptable, repair_forbidden, recovered, policy_violations,
                           loop_success, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        payload["run_id"],
                        payload["scorer_version"],
                        payload["diagnosis"],
                        self._db_value("diagnosis_correct", payload["diagnosis_correct"]),
                        payload["repair_action"],
                        self._db_value("repair_acceptable", payload["repair_acceptable"]),
                        self._db_value("repair_forbidden", payload["repair_forbidden"]),
                        self._db_value("recovered", payload["recovered"]),
                        payload["policy_violations"],
                        self._db_value("loop_success", payload["loop_success"]),
                        payload["created_at"],
                    ),
                )
        self.release_lease(run_id)
        return self.get_run(run_id)

    def load_records(
        self, include_resumed: bool = False, include_aborted: bool = False
    ) -> list[dict[str, Any]]:
        clauses = ["status='finished'"]
        if not include_resumed:
            clauses.append("resume_count=0")
        if not include_aborted:
            clauses.append("(end_reason IS NULL OR end_reason != 'legacy_exploratory')")
        sql = "SELECT * FROM runs WHERE " + " AND ".join(clauses)
        sql += " ORDER BY COALESCE(started_at, 0), run_id"
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [self.record_from_row(dict(row)) for row in rows if row["truth"] is not None]

    def export_all(self, runs_path: str | Path, trace_dir: str | Path) -> dict[str, Any]:
        runs_path = Path(runs_path)
        trace_dir = Path(trace_dir)
        runs_path.parent.mkdir(parents=True, exist_ok=True)
        trace_dir.mkdir(parents=True, exist_ok=True)
        records = self.load_records(include_resumed=True)
        runs_path.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )
        with self._connect() as conn:
            run_ids = [row["run_id"] for row in conn.execute("SELECT run_id FROM runs")]
        trace_count = 0
        for run_id in run_ids:
            events = self.load_events(run_id)
            if not events:
                continue
            target = trace_dir / f"{run_id}.jsonl"
            target.write_text(
                "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
                encoding="utf-8",
            )
            trace_count += 1
        return {"runs": len(records), "traces": trace_count}

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

            self.create_run(
                str(run_id),
                batch_id=batch_id,
                scenario_id=scenario_id,
                scenario_version=version,
                spec_hash="legacy",
                backend=str(record.get("backend", "unknown")),
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
            "trace_events", "run_artifacts", "diagnosis_attempts", "policy_violations",
            "scores", "score_reports",
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
