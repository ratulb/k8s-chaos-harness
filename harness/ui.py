from __future__ import annotations

import json
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, TextIO

from storage.sqlite_store import read_only_rows
from tools import topology as topology_tools
from tools.truncate import truncate


class LiveView:
    def __init__(
        self,
        db_path: str | Path,
        run_id: str,
        interval: float = 0.5,
        stream: TextIO | None = None,
        mode: str = "benchmark",
    ):
        self.db_path = Path(db_path)
        self.run_id = run_id
        self.interval = max(0.1, float(interval))
        self.stream = stream or sys.stdout
        self.mode = mode
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _poll(self) -> None:
        while not self._stop.wait(self.interval):
            self.render_once()

    def _rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return read_only_rows(self.db_path, sql, params)

    def render_once(self) -> None:
        text = self.render_text()
        try:
            isatty = bool(getattr(self.stream, "isatty", lambda: False)())
        except Exception:
            isatty = False
        if isatty:
            self.stream.write("\x1b[H\x1b[2J")
        self.stream.write(text + "\n")
        self.stream.flush()

    def render_text(self) -> str:
        width = shutil.get_terminal_size((120, 40)).columns
        runs = self._rows("SELECT * FROM runs WHERE run_id=?", (self.run_id,))
        if not runs:
            return f"run {self.run_id}: waiting for database row"
        run = runs[0]
        topology_hash = run.get("topology_hash") or "-"
        header = (
            f"{self.run_id} | {run.get('scenario_id', '?')}/{run.get('backend', '?')} | "
            f"{self.mode} | topology={topology_hash}"
        )
        started = float(run.get("started_at") or time.time())
        now = time.time()
        elapsed = max(0.0, now - started)
        phase = run.get("phase") or run.get("status") or "?"
        status = run.get("status") or "?"
        steps = int(run.get("steps") or 0)
        tokens = int(run.get("tokens_used") or 0)

        chunks = self._rows(
            "SELECT seq, ts, kind, text FROM ui_stream WHERE run_id=? ORDER BY seq DESC LIMIT 8",
            (self.run_id,),
        )
        chunks.reverse()
        events = self._rows(
            "SELECT seq, ts, type, payload_json FROM trace_events WHERE run_id=? ORDER BY seq DESC LIMIT 8",
            (self.run_id,),
        )
        events.reverse()
        snapshots = self._rows(
            "SELECT ts, source, state_json FROM state_snapshots WHERE run_id=? ORDER BY snapshot_seq DESC LIMIT 1",
            (self.run_id,),
        )
        artifacts = self._rows(
            "SELECT kind, content FROM run_artifacts WHERE run_id=? AND kind='topology_full' "
            "ORDER BY seq DESC LIMIT 1",
            (self.run_id,),
        )

        latest = [float(run.get("updated_at") or 0)]
        latest.extend(float(chunk.get("ts") or 0) for chunk in chunks)
        latest.extend(float(event.get("ts") or 0) for event in events)
        age = now - max(latest) if latest else 0.0
        stall = "  STALL" if age > 30 else ""

        snapshot: dict[str, Any] = {}
        if snapshots:
            try:
                parsed = json.loads(snapshots[0].get("state_json") or "{}")
                if isinstance(parsed, dict):
                    snapshot = parsed
            except (TypeError, ValueError):
                snapshot = {}
        diagnosis = snapshot.get("diagnosis") or run.get("diagnosis") or "-"
        repair = snapshot.get("repair") or run.get("repair") or "-"

        topology_line = f"cluster: topology={topology_hash}"
        if artifacts:
            try:
                topology_data = json.loads(artifacts[0].get("content") or "{}")
                summary = topology_tools.summary(topology_data)
                nodes = ", ".join(summary.get("control_planes") or [])
                topology_line = (
                    f"cluster: nodes={nodes} members={summary.get('etcd_member_count', '?')} "
                    f"quorum={summary.get('quorum', '?')} source={topology_data.get('source', '?')}"
                )
            except (TypeError, ValueError, KeyError):
                pass

        stream_tail = " | ".join(
            f"{chunk.get('kind')}: {chunk.get('text')}" for chunk in chunks[-3:]
        ) or "waiting for model output"
        tool_events = []
        for event in events:
            if event.get("type") != "tool":
                continue
            try:
                payload = json.loads(event.get("payload_json") or "{}")
            except (TypeError, ValueError):
                payload = {}
            tool_events.append(f"{payload.get('name', '?')}:{payload.get('status', '?')}")
        tool_tail = " | ".join(tool_events[-4:]) or "no tools yet"
        event_tail = " | ".join(
            f"{event.get('seq')}:{event.get('type')}" for event in events[-5:]
        ) or "no events yet"

        lines = [
            truncate(header, width),
            f"phase={phase} status={status} elapsed={elapsed:6.1f}s steps={steps} tokens={tokens}{stall}",
            f"diagnosis={diagnosis} repair={repair} end_reason={run.get('end_reason') or '-'}",
            truncate(topology_line, width),
            f"stream: {truncate(stream_tail, max(40, width - 9))}",
            f"tools: {truncate(tool_tail, max(40, width - 8))}",
            f"events: {truncate(event_tail, max(40, width - 9))}",
        ]
        return "\n".join(line.rstrip() for line in lines)
