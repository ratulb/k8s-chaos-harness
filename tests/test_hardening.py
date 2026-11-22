from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import chaos_cli
import run_benchmark
import score
from common.errors import LLMError, classify_llm_error
from common.vocab import DESTRUCTIVE_TOOLS, DIAGNOSIS_TOOL, REPAIR_TOOLS
from storage import sqlite_store
from tools import schemas
from harness import agent
from storage.sqlite_store import SQLiteStore
from tools import fault_checks, safety


def make_record(run_id: str, **overrides: object) -> dict:
    record = {
        "run_id": run_id,
        "scenario": "etcd-member-down",
        "backend": "fake",
        "truth": "etcd_member_unreachable",
        "diagnosis": "etcd_member_unreachable",
        "diagnosis_correct": True,
        "approval_granted": True,
        "verify_ok": True,
        "repair": "restart_member",
        "repair_acceptable": True,
        "repair_forbidden": False,
        "policy_violations": [],
        "fault_present": True,
        "end_reason": "submitted",
        "steps": 5,
        "tokens_used": 100,
    }
    record.update(overrides)
    return record


class FakeBackend:
    name = "fake"


class FakeClient:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)

    def chat(self, messages: list[dict], **kwargs: object) -> dict:
        return self.responses.pop(0)


def assistant(tool_calls: list[dict]) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": "", "tool_calls": tool_calls}}],
        "usage": {"total_tokens": 10},
    }


def tool_call(name: str, args: dict) -> dict:
    return {"id": "call-1", "function": {"name": name, "arguments": json.dumps(args)}}


class TestHardening(unittest.TestCase):
    def test_strict_metric_and_provider_exclusion(self) -> None:
        records = [
            make_record("good"),
            make_record("provider", end_reason="provider_error_429"),
            make_record("no-fault", fault_present=False),
            make_record("forbidden", repair_forbidden=True),
        ]
        stats = score.summarize(records)
        self.assertEqual(stats["strict_loop_success"], 0.25)
        self.assertTrue(score.strict_success(records[0]))
        self.assertFalse(score.strict_success(records[1]))
        self.assertFalse(score.strict_success(records[2]))
        self.assertFalse(score.strict_success(records[3]))

    def test_legacy_loader_filters_provider_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.jsonl"
            path.write_text(
                "".join(json.dumps(item) + "\n" for item in [
                    make_record("good"),
                    make_record("provider", end_reason="provider_error_413"),
                ]),
                encoding="utf-8",
            )
            self.assertEqual([r["run_id"] for r in score.load(str(path))], ["good"])
            self.assertEqual(
                [r["run_id"] for r in score.load(str(path), include_provider_errors=True)],
                ["good", "provider"],
            )

    def test_db_loader_filters_provider_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(Path(tmp) / "results.sqlite3")
            store.create_run("provider", scenario_id="etcd-member-down", backend="fake", truth="etcd_member_unreachable")
            store.finish_run(
                "provider",
                make_record("provider"),
                status="finished",
                end_reason="provider_error_429",
            )
            self.assertEqual(store.load_records(), [])
            self.assertEqual(
                [r["run_id"] for r in store.load_records(include_provider_errors=True)],
                ["provider"],
            )
            store.close()

    def test_provider_error_classification(self) -> None:
        self.assertEqual(classify_llm_error("HTTP Error 413: Payload Too Large"), "provider_error_413")
        self.assertEqual(classify_llm_error("HTTP Error 429: Too Many Requests"), "provider_error_429")
        self.assertEqual(classify_llm_error("HTTP Error 502: Bad Gateway"), "provider_error_5xx")
        self.assertEqual(classify_llm_error("boom"), "error")

    def test_llm_error_object_classifies_structured_failures(self) -> None:
        error = LLMError.from_exception(urllib.error.HTTPError("http://x", 429, "slow down", {}, None))
        self.assertEqual(error.kind, "http")
        self.assertEqual(error.status_code, 429)
        self.assertEqual(error.classify(), "provider_error_429")
        self.assertEqual(error.end_reason, "provider_error_429")
        self.assertEqual(classify_llm_error(error), "provider_error_429")
        self.assertEqual(classify_llm_error(TimeoutError("late")), "error")

    def test_shared_tool_vocabulary_has_one_source(self) -> None:
        self.assertIs(schemas.REPAIR_TOOLS, REPAIR_TOOLS)
        self.assertIs(sqlite_store.REPAIR_TOOLS, REPAIR_TOOLS)
        self.assertIs(schemas.DIAGNOSIS_TOOL, DIAGNOSIS_TOOL)
        self.assertIs(sqlite_store.DIAGNOSIS_TOOL, DIAGNOSIS_TOOL)
        self.assertIs(sqlite_store.DESTRUCTIVE_TOOLS, DESTRUCTIVE_TOOLS)

    def test_safety_guard_enforces_live_and_fixture_runs(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch("tools.safety.current_context", return_value="prod"):
            with self.assertRaises(RuntimeError):
                safety.require_destructive_guard({"source": "kubectl", "topology_hash": "abc"})
            with self.assertRaises(RuntimeError):
                safety.require_destructive_guard({"source": "fixture", "topology_hash": "abc"})
        with patch.dict(
            os.environ,
            {"CHAOS_ALLOWED_CLUSTER_CONTEXTS": "lab", "CHAOS_ALLOWED_TOPOLOGY_HASHES": "abc"},
            clear=True,
        ), patch("tools.safety.current_context", return_value="lab"):
            allowed = safety.require_destructive_guard({"source": "fixture", "topology_hash": "abc"})
        self.assertTrue(allowed["enforced"])

    def test_etcd_member_list_uses_supported_write_out_flag(self) -> None:
        payload = json.dumps({"members": [{"name": "node-b", "peerURLs": ["https://10.0.0.2:2399"]}]})
        with patch("tools.fault_checks._probe_node", return_value="node-a"), \
             patch("tools.fault_checks.cluster_config.node_ip", return_value="10.0.0.2"), \
             patch("tools.fault_checks._kubectl", return_value=(True, payload)) as mock_kubectl:
            present, _ = fault_checks.check_fault_present({"id": "etcd-wrong-peer-url"}, ["node-b"])
        self.assertTrue(present)
        args = mock_kubectl.call_args.args[0]
        self.assertIn("--write-out=json", args)
        self.assertNotIn("-o", args)

    def test_wiped_fault_detects_missing_data_dir(self) -> None:
        with patch("tools.fault_checks.run_on_node", return_value=(0, "DB_MISSING:0\n0", "")), \
             patch("tools.fault_checks._pod_ready", return_value=False):
            present, _ = fault_checks.check_fault_present({"id": "etcd-member-wiped"}, ["node-b"])
        self.assertTrue(present)

    def test_fault_timeout_and_bounded_wait(self) -> None:
        scenario = {"id": "etcd-member-down", "fault_present_timeout_seconds": 7}
        self.assertEqual(fault_checks.timeout_for(scenario), 7.0)
        with patch(
            "tools.fault_checks.check_fault_present",
            side_effect=[(False, "waiting"), (True, "down")],
        ), patch("tools.fault_checks.time.sleep") as sleeper:
            present, detail, _ = fault_checks.wait_for_fault(scenario, ["node-b"])
        self.assertTrue(present)
        self.assertEqual(detail, "down")
        sleeper.assert_called_once()

    def test_fault_predicates(self) -> None:
        with patch("tools.fault_checks._pod_ready", return_value=False):
            present, _ = fault_checks.check_fault_present({"id": "etcd-member-down"}, ["node-b"])
        self.assertTrue(present)
        with patch("tools.fault_checks._probe_node", return_value="node-a"), \
             patch("tools.fault_checks.cluster_config.node_ip", return_value="10.0.0.2"), \
             patch("tools.fault_checks._members", return_value=[
                 {"name": "node-b", "peerURLs": ["https://10.0.0.2:2399"]},
             ]):
            present, _ = fault_checks.check_fault_present({"id": "etcd-wrong-peer-url"}, ["node-b"])
        self.assertTrue(present)
        present, _ = fault_checks.check_fault_present({"id": "control-healthy-noisy"}, [])
        self.assertTrue(present)

    def test_no_fault_strict_success_from_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(Path(tmp) / "results.sqlite3")
            store.create_run(
                "control",
                scenario_id="control-healthy-noisy",
                backend="fake",
                truth="no_fault",
            )
            record = make_record(
                "control",
                scenario="control-healthy-noisy",
                truth="no_fault",
                diagnosis="no_fault",
                repair=None,
                repair_acceptable=True,
            )
            store.finish_run(
                "control",
                record,
                status="finished",
                score={"scorer_version": "test-v1", "repair_acceptable": True},
            )
            loaded = store.load_records()
            self.assertEqual(len(loaded), 1)
            self.assertTrue(score.strict_success(loaded[0]))
            store.close()

    def test_agent_accepts_no_fault_without_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(Path(tmp) / "results.sqlite3")
            store.create_run(
                "control-agent",
                scenario_id="control-healthy-noisy",
                backend="fake",
                truth="no_fault",
            )
            responses = [
                assistant([tool_call("submit_diagnosis", {"root_cause": "no_fault"})]),
                assistant([]),
            ]
            scenario = {
                "id": "control-healthy-noisy",
                "root_cause": "no_fault",
                "acceptable_repairs": [],
                "forbidden_repairs": ["restart_member"],
                "max_steps": 5,
                "max_tokens": 100,
            }
            with patch("harness.agent.LLMClient", lambda backend: FakeClient(responses)), \
                 patch("harness.agent.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ok", "")):
                agent.run_scenario(
                    scenario,
                    FakeBackend(),
                    "control-agent",
                    "",
                    "true",
                    trace_dir=str(Path(tmp) / "traces"),
                    store=store,
                )
            with store._connect() as conn:
                acceptable = conn.execute(
                    "SELECT repair_acceptable FROM scores WHERE run_id=?", ("control-agent",)
                ).fetchone()[0]
            self.assertEqual(acceptable, 1)
            store.close()

    def test_fresh_run_aborts_when_fault_is_not_observed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(Path(tmp) / "results.sqlite3")
            row = store.create_run(
                "fault-check",
                scenario_id="etcd-member-down",
                backend="fake",
                truth="etcd_member_unreachable",
            )
            scenario = {
                "id": "etcd-member-down",
                "root_cause": "etcd_member_unreachable",
                "timeout_seconds": 30,
                "fault_present_timeout_seconds": 1,
            }
            with patch("run_benchmark._run", return_value=(True, "ok")), \
                 patch("run_benchmark.fault_checks.wait_for_fault", return_value=(False, "still ready", 0.2)):
                record = run_benchmark._run_fresh(
                    store,
                    row,
                    scenario,
                    SimpleNamespace(name="fake"),
                    "true",
                    "true",
                    "",
                    {"TARGET_NODES": "node-b"},
                    30,
                    str(Path(tmp) / "traces"),
                    None,
                )
            self.assertEqual(record["end_reason"], "fault_not_observed")
            self.assertFalse(record["fault_present"])
            status = store.get_run("fault-check")["status"]
            self.assertEqual(status, "aborted")
            with store._connect() as conn:
                kinds = {row["kind"] for row in conn.execute(
                    "SELECT kind FROM run_artifacts WHERE run_id=?", ("fault-check",)
                )}
            self.assertIn("fault_check", kinds)
            store.close()

    def test_menu_uses_chaos_run(self) -> None:
        script = chaos_cli._menu_script([{"id": "etcd-member-down", "label": "Etcd member down"}])
        self.assertIn('exec ./chaos run --scenario "$id" --backend "$BACKEND" "$@"', script)
        self.assertNotIn("run_benchmark.sh", script)


if __name__ == "__main__":
    unittest.main(verbosity=2)
