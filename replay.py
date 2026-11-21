from __future__ import annotations

import argparse
import html
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from harness.reporter import redact
from storage.sqlite_store import SQLiteStore, read_only_rows
from tools import topology as topology_tools


def _escape(value: Any) -> str:
    return html.escape(redact(str(value)), quote=True)


def _json_text(value: Any) -> str:
    return _escape(json.dumps(value, sort_keys=True, default=str))


def _payload_text(raw: Any) -> str:
    if isinstance(raw, str):
        try:
            return _json_text(json.loads(raw))
        except (TypeError, ValueError):
            return _escape(raw)
    return _json_text(raw)


def _topology_class(content: str | None) -> str:
    if not content:
        return "unknown"
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        return "unknown"
    count = data.get("etcd_member_count") or len(data.get("etcd_members") or [])
    source = data.get("source") or "unknown"
    return f"n{count}-{source}" if count else f"unknown-{source}"


def _matrix_rows(store: SQLiteStore, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    topology_by_run = {
        row["run_id"]: _topology_class(row.get("content"))
        for row in read_only_rows(
            store.path,
            "SELECT run_id, content FROM run_artifacts WHERE kind='topology_full' ORDER BY seq",
        )
    }
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = defaultdict(
        lambda: {"runs": 0, "diagnosed": 0, "repaired": 0, "verified": 0}
    )
    for rec in records:
        key = (
            str(rec.get("scenario") or rec.get("scenario_id") or "unknown"),
            str(rec.get("backend") or "unknown"),
            topology_by_run.get(str(rec.get("run_id")), "unknown"),
            str(rec.get("seed", "")),
        )
        row = grouped[key]
        row["runs"] += 1
        row["diagnosed"] += int(bool(rec.get("diagnosis_correct")))
        repaired = bool(rec.get("repair"))
        row["repaired"] += int(repaired)
        row["verified"] += int(bool(rec.get("verify_ok")))
    output: list[dict[str, Any]] = []
    for key, row in sorted(grouped.items()):
        scenario, backend, topology, seed = key
        runs = row["runs"]
        output.append({
            "scenario": scenario,
            "backend": backend,
            "topology": topology,
            "seed": seed,
            "runs": runs,
            "diagnosis_rate": row["diagnosed"] / runs,
            "repair_rate": row["repaired"] / runs,
            "verify_rate": row["verified"] / runs,
        })
    return output


def _run_sections(db_path: str | Path, allowed: set[str] | None = None) -> list[str]:
    sections: list[str] = []
    runs = read_only_rows(db_path, "SELECT * FROM runs ORDER BY started_at, run_id")
    for run in runs:
        run_id = str(run["run_id"])
        if allowed is not None and run_id not in allowed:
            continue
        events = read_only_rows(
            db_path,
            "SELECT seq, ts, type, payload_json FROM trace_events WHERE run_id=? ORDER BY seq",
            (run_id,),
        )
        chunks = read_only_rows(
            db_path,
            "SELECT seq, ts, kind, text FROM ui_stream WHERE run_id=? ORDER BY seq",
            (run_id,),
        )
        final = read_only_rows(
            db_path,
            "SELECT content FROM run_artifacts WHERE run_id=? AND kind='final_record' ORDER BY seq DESC LIMIT 1",
            (run_id,),
        )
        event_lines = "".join(
            f"<li><code>{int(event['seq'])}</code> {_escape(event['type'])} "
            f"{_payload_text(event.get('payload_json'))}</li>"
            for event in events
        )
        chunk_lines = "".join(
            f"<li><code>{int(chunk['seq'])}</code> {_escape(chunk['kind'])}: "
            f"{_escape(chunk['text'])}</li>"
            for chunk in chunks
        )
        final_text = _payload_text(final[0]["content"]) if final else "not recorded"
        topology_rows = read_only_rows(
            db_path,
            "SELECT content FROM run_artifacts WHERE run_id=? AND kind='topology_full' ORDER BY seq DESC LIMIT 1",
            (run_id,),
        )
        topology_text = "not recorded"
        if topology_rows:
            try:
                summary = topology_tools.summary(json.loads(topology_rows[0]["content"]))
                topology_text = "; ".join([
                    f"source={summary.get('source', '?')}",
                    f"nodes={', '.join(summary.get('control_planes') or []) or '-'}",
                    f"members={summary.get('etcd_member_count', '?')}",
                    f"quorum={summary.get('quorum', '?')}",
                    f"quorum_loss={', '.join(summary.get('quorum_loss_nodes') or []) or '-'}",
                ])
            except (TypeError, ValueError, KeyError):
                pass
        sections.append(
            "<details>"
            f"<summary>{_escape(run_id)} — {_escape(run.get('status'))} / "
            f"{_escape(run.get('phase'))} — {_escape(run.get('scenario_id'))} / "
            f"{_escape(run.get('backend'))}</summary>"
            f"<h4>Topology</h4><p>{_escape(topology_text)}</p>"
            f"<h4>Stream</h4><ol>{chunk_lines or '<li>no stream chunks</li>'}</ol>"
            f"<h4>Events</h4><ol>{event_lines or '<li>no events</li>'}</ol>"
            f"<h4>Final record</h4><pre>{final_text}</pre>"
            "</details>"
        )
    return sections


def build_html(db_path: str | Path, include_interactive: bool = False) -> str:
    store = SQLiteStore(db_path)
    try:
        records = store.load_records(include_resumed=False, include_interactive=include_interactive)
        matrix = _matrix_rows(store, records)
    finally:
        store.close()
    matrix_lines = "".join(
        "<tr>"
        f"<td>{_escape(row['scenario'])}</td>"
        f"<td>{_escape(row['backend'])}</td>"
        f"<td>{_escape(row['topology'])}</td>"
        f"<td>{_escape(row['seed'])}</td>"
        f"<td>{row['runs']}</td>"
        f"<td>{row['diagnosis_rate']:.2f}</td>"
        f"<td>{row['repair_rate']:.2f}</td>"
        f"<td>{row['verify_rate']:.2f}</td>"
        "</tr>"
        for row in matrix
    )
    sections = _run_sections(db_path)
    return "\n".join([
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Chaos harness replay</title>",
        "<style>",
        "body{font:14px/1.5 system-ui,sans-serif;margin:2rem;color:#222}",
        "table{border-collapse:collapse;width:100%;margin-bottom:2rem}",
        "th,td{border:1px solid #ccc;padding:.4rem .6rem;text-align:left}",
        "details{border:1px solid #ddd;border-radius:4px;padding:.5rem;margin:.5rem 0}",
        "summary{cursor:pointer;font-weight:600}",
        "pre{white-space:pre-wrap;word-break:break-word;background:#f6f6f6;padding:.6rem}",
        "li{margin:.2rem 0}",
        "</style></head><body>",
        "<h1>Chaos harness replay</h1>",
        "<p>Static replay from SQLite. Event and stream text is redacted before rendering.</p>",
        "<h2>Results matrix</h2>",
        "<table><thead><tr><th>Scenario</th><th>Backend</th><th>Topology</th><th>Seed</th>"
        "<th>Runs</th><th>Diagnosis</th><th>Repair</th><th>Verify</th></tr></thead>"
        f"<tbody>{matrix_lines}</tbody></table>",
        "<h2>Runs</h2>",
        *sections,
        "</body></html>",
    ])


def write_replay(db_path: str | Path, output: str | Path, include_interactive: bool = False) -> dict[str, Any]:
    target = Path(output)
    if target.parent != Path("."):
        target.parent.mkdir(parents=True, exist_ok=True)
    text = build_html(db_path, include_interactive)
    target.write_text(text, encoding="utf-8")
    return {"database": str(db_path), "output": str(target), "bytes": len(text.encode("utf-8"))}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="replay")
    ap.add_argument("--db", default="results.sqlite3")
    ap.add_argument("--output", default="replay.html")
    ap.add_argument("--include-interactive", action="store_true")
    args = ap.parse_args(argv)
    result = write_replay(args.db, args.output, args.include_interactive)
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
