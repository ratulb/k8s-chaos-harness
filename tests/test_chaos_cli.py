from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CHAOS = ROOT / "chaos"
sys.path.insert(0, str(ROOT))

import chaos_cli
from storage.sqlite_store import SQLiteStore


class TestChaosCli(unittest.TestCase):
    def _seed_run(self, db: Path, topology_hash: str = "old-topology", pid: int = -1) -> None:
        store = SQLiteStore(db)
        scenario = {"id": "etcd-member-down", "root_cause": "etcd_member_unreachable"}
        spec_hash = store.hash_spec(scenario)
        version = store.ensure_scenario(scenario["id"], spec_hash, json.dumps(scenario))
        store.create_run(
            "run-1",
            scenario_id=scenario["id"],
            scenario_version=version,
            spec_hash=spec_hash,
            backend="ollama",
            truth=scenario["root_cause"],
            status="running",
            phase="agent_running",
            topology_hash=topology_hash,
            pid=pid,
        )
        store.add_event("run-1", "topology", {"topology_hash": topology_hash})
        store.close()

    def _cli(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CHAOS), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )

    def test_target_show_json(self) -> None:
        env = os.environ.copy()
        env["CHAOS_TOPOLOGY_JSON"] = str(ROOT / "tests" / "fixtures" / "topologies" / "n4.json")
        result = self._cli("target", "show", "--json", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["topology"]["etcd_member_count"], 4)
        self.assertEqual(payload["topology"]["quorum"], 3)
        self.assertEqual(len(payload["capability"]), 6)
        self.assertIn("etcd-quorum-loss", payload["capability"])
        human = self._cli("target", "show", env=env)
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn("3 is the loud recommendation", human.stdout)

    def test_scenario_list_json(self) -> None:
        result = self._cli("scenario", "list", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(len(payload), 6)
        self.assertTrue(all("risk" in item and "impact_class" in item for item in payload))

    def test_scenario_show_json(self) -> None:
        result = self._cli("scenario", "show", "etcd-member-down", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["id"], "etcd-member-down")
        self.assertEqual(payload["risk"], "disruptive")

    def test_scenario_validate_all(self) -> None:
        result = self._cli("scenario", "validate", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["scenarios"], 6)
        self.assertEqual(payload["errors"], {})

    def test_scenario_menu_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "menu.sh"
            result = self._cli("scenario", "menu", "--output", str(output))
            self.assertEqual(result.returncode, 0, result.stderr)
            text = output.read_text(encoding="utf-8")
            self.assertIn('BACKEND="${BACKEND:-ollama}"', text)
            self.assertIn("--backend \"$BACKEND\"", text)
            self.assertIn("etcd-quorum-loss", text)
            self.assertTrue(os.access(output, os.X_OK))
            syntax = subprocess.run(["bash", "-n", str(output)], capture_output=True, text=True)
            self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_state_discard_clears_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "results.sqlite3"
            self._seed_run(db)
            runs_file = root / "runs.jsonl"
            runs_file.write_text('{"run_id":"run-1"}\n', encoding="utf-8")
            traces = root / "traces"
            traces.mkdir()
            trace = traces / "run-1.jsonl"
            trace.write_text('{}\n', encoding="utf-8")
            cache = root / ".cache" / "k8s-chaos-harness" / "node_ips.json"
            cache.parent.mkdir(parents=True)
            cache.write_text("{}\n", encoding="utf-8")
            env = os.environ.copy()
            env["HOME"] = str(root)

            result = self._cli(
                "--db", str(db),
                "--runs-file", str(runs_file),
                "--trace-dir", str(traces),
                "state", "discard", "--yes",
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertIn("run-1", summary["runs_aborted"])
            self.assertTrue(summary["reinitialized"])
            self.assertEqual(runs_file.read_text(encoding="utf-8"), "")
            self.assertFalse(trace.exists())
            self.assertFalse(cache.exists())
            check = SQLiteStore(db)
            try:
                self.assertEqual(check.doctor()["counts"]["runs"], 0)
            finally:
                check.close()

    def test_read_only_commands_warn_without_discarding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "results.sqlite3"
            self._seed_run(db)
            missing_db = root / "missing.sqlite3"
            env = os.environ.copy()
            env["HOME"] = str(root)
            env["CHAOS_TOPOLOGY_JSON"] = str(ROOT / "tests" / "fixtures" / "topologies" / "n4.json")

            for args in (
                ("--db", str(db), "status", "--json"),
                ("--db", str(db), "attach", "run-1", "--json"),
                ("--db", str(db), "target", "show", "--json"),
            ):
                result = self._cli(*args, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                self.assertTrue(payload["warnings"])
                self.assertTrue(any("differs" in warning for warning in payload["warnings"]))

            missing = self._cli("--db", str(missing_db), "status", "--json", env=env)
            self.assertEqual(missing.returncode, 0, missing.stderr)
            self.assertFalse(missing_db.exists())
            self.assertIn("no active database", json.loads(missing.stdout)["warnings"][0])

            check = SQLiteStore(db)
            try:
                row = check.get_run("run-1")
                self.assertEqual(row["topology_hash"], "old-topology")
                self.assertEqual(len(check.load_events("run-1")), 1)
            finally:
                check.close()

    def test_abort_marks_run_aborted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "results.sqlite3"
            self._seed_run(db)
            result = self._cli("--db", str(db), "abort", "run-1", "--no-kill")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["aborted"], ["run-1"])
            check = SQLiteStore(db)
            try:
                self.assertEqual(check.get_run("run-1")["status"], "aborted")
                self.assertTrue(any(e["type"] == "operator_abort" for e in check.load_events("run-1")))
            finally:
                check.close()

    def test_abandon_moves_interrupted_run_to_aborted(self) -> None:
        from harness.reaper import reap_expired

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "results.sqlite3"
            self._seed_run(db)
            store = SQLiteStore(db)
            with store._connect() as conn:
                conn.execute(
                    "UPDATE runs SET lease_expires_at=1.0 WHERE run_id='run-1'"
                )
            store.close()
            reap_expired(str(db), now=2.0)
            result = self._cli("--db", str(db), "abort", "run-1", "--abandon")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "aborted")
            self.assertEqual(payload["end_reason"], "abandoned")
            check = SQLiteStore(db)
            try:
                self.assertEqual(check.get_run("run-1")["status"], "aborted")
                self.assertTrue(any(e["type"] == "run.abandon" for e in check.load_events("run-1")))
            finally:
                check.close()

    def test_abort_no_kill_refuses_live_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "results.sqlite3"
            self._seed_run(db, pid=os.getpid())
            result = self._cli("--db", str(db), "abort", "run-1", "--no-kill")
            self.assertEqual(result.returncode, 1)
            self.assertIn("refusing abort while active runs are alive", result.stderr)
            check = SQLiteStore(db)
            try:
                self.assertEqual(check.get_run("run-1")["status"], "running")
            finally:
                check.close()

    def test_attach_follow_stops_on_terminal_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "results.sqlite3"
            self._seed_run(db, topology_hash="t")
            store = SQLiteStore(db)
            store.append_ui_chunk("run-1", "text", "hello")
            store.finish_run("run-1", status="finished")
            store.close()
            env = os.environ.copy()
            env["CHAOS_TOPOLOGY_JSON"] = str(ROOT / "tests" / "fixtures" / "topologies" / "n3.json")
            result = self._cli(
                "--db", str(db), "attach", "run-1", "--follow", "--interval", "0.1", env=env
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("hello", result.stdout)

    def test_replay_writes_static_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "results.sqlite3"
            output = root / "reports" / "replay.html"
            self._seed_run(db)
            result = self._cli("--db", str(db), "replay", "--output", str(output))
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["output"], str(output))
            self.assertTrue(output.exists())
            self.assertIn("run-1", output.read_text(encoding="utf-8"))

    def test_run_detach_returns_launch_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "results.sqlite3"
            logs = root / "logs"
            output = io.StringIO()
            with patch("chaos_cli.subprocess.Popen") as popen:
                popen.return_value.pid = 4242
                with redirect_stdout(output):
                    code = chaos_cli.main([
                        "--db", str(db),
                        "run",
                        "--scenario", "etcd-member-down",
                        "--backend", "ollama",
                        "--approve", "prompt",
                        "--batch-id", "sweep-1",
                        "--detach",
                        "--ui", "none",
                        "--logs-dir", str(logs),
                    ])
            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["pid"], 4242)
            self.assertTrue(Path(payload["log"]).parent.exists())
            self.assertIn("--approve", payload["command"])
            self.assertEqual(payload["command"][payload["command"].index("--approve") + 1], "prompt")
            self.assertEqual(payload["command"][payload["command"].index("--batch-id") + 1], "sweep-1")
            self.assertIn("--launch-id", payload["command"])
            self.assertTrue(payload["command"][-1].startswith("launch-"))
            self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_ssh_preflight_skips_local_host(self) -> None:
        with patch("chaos_cli._local_addresses", return_value={"10.2.0.4"}), \
             patch("chaos_cli.cluster_config.ssh_user", return_value="azureuser"), \
             patch("chaos_cli.cluster_config.ssh_key", return_value=""), \
             patch("chaos_cli.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as runner:
            results = chaos_cli._ssh_preflight(["10.2.0.4", "10.1.0.4"])
        self.assertTrue(results[0]["local"])
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[1].get("local", False))
        runner.assert_called_once()

    def test_cluster_nuke_success_discards_only_after_rebuild(self) -> None:
        setup = {"path": "/tmp/setup.conf", "masters": ["10.1.0.4"], "hash": "abc123"}
        topology_data = {"node_details": [{"name": "vm1", "ip": "10.1.0.4"}]}
        output = io.StringIO()
        with patch("chaos_cli.topology_tools.setup_conf_summary", return_value=setup), \
             patch("chaos_cli._rebuild_lock", return_value=nullcontext(Path("rebuild.lock"))), \
             patch("chaos_cli._ssh_preflight", return_value=[{"host": "10.1.0.4", "ok": True, "detail": ""}]), \
             patch("chaos_cli.store_tools.abort_runs", return_value={"aborted": []}), \
             patch("chaos_cli.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as rebuild, \
             patch("chaos_cli.topology_tools.clear_frozen_topology"), \
             patch("chaos_cli.topology_tools.discover_topology", return_value=topology_data), \
             patch("chaos_cli.topology_tools.summary", return_value={"topology_hash": "live"}), \
             patch("chaos_cli.cluster_config.cache_node_ips"), \
             patch("chaos_cli.cluster_config.clear_ip_cache"), \
             patch("chaos_cli.store_tools.discard_state", return_value={"reinitialized": True}) as discard:
            with redirect_stdout(output):
                code = chaos_cli.main([
                    "--db", "/tmp/results.sqlite3",
                    "cluster", "nuke", "--yes",
                ])
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["topology"], {"topology_hash": "live"})
        self.assertTrue(payload["state"]["reinitialized"])
        rebuild.assert_called_once()
        discard.assert_called_once()

    def test_cluster_nuke_preflight_failure_skips_rebuild(self) -> None:
        setup = {"path": "/tmp/setup.conf", "masters": ["10.1.0.4"], "hash": "abc123"}
        output = io.StringIO()
        with patch("chaos_cli.topology_tools.setup_conf_summary", return_value=setup), \
             patch("chaos_cli._rebuild_lock", return_value=nullcontext(Path("rebuild.lock"))), \
             patch("chaos_cli._ssh_preflight", return_value=[{"host": "10.1.0.4", "ok": False, "detail": "timeout"}]), \
             patch("chaos_cli.subprocess.run") as rebuild, \
             patch("chaos_cli.store_tools.discard_state") as discard:
            with redirect_stdout(output):
                code = chaos_cli.main([
                    "--db", "/tmp/results.sqlite3",
                    "cluster", "nuke", "--yes",
                ])
        self.assertEqual(code, 2)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "preflight_failed")
        rebuild.assert_not_called()
        discard.assert_not_called()

    def test_cluster_nuke_rebuild_failure_preserves_state(self) -> None:
        setup = {"path": "/tmp/setup.conf", "masters": ["10.1.0.4"], "hash": "abc123"}
        output = io.StringIO()
        with patch("chaos_cli.topology_tools.setup_conf_summary", return_value=setup), \
             patch("chaos_cli._rebuild_lock", return_value=nullcontext(Path("rebuild.lock"))), \
             patch("chaos_cli._ssh_preflight", return_value=[{"host": "10.1.0.4", "ok": True, "detail": ""}]), \
             patch("chaos_cli.store_tools.abort_runs", return_value={"aborted": []}), \
             patch("chaos_cli.subprocess.run", return_value=subprocess.CompletedProcess([], 1)), \
             patch("chaos_cli.store_tools.discard_state") as discard:
            with redirect_stdout(output):
                code = chaos_cli.main([
                    "--db", "/tmp/results.sqlite3",
                    "cluster", "nuke", "--yes",
                ])
        self.assertEqual(code, 1)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "rebuild_failed")
        self.assertEqual(payload["state"], "preserved")
        discard.assert_not_called()

    def test_ping_reports_missing_llm_config_without_traceback(self) -> None:
        stderr = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stderr(stderr):
            code = chaos_cli._ping("auto", "hello")
        self.assertEqual(code, 1)
        self.assertIn("LLM configuration error", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
