from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.results import Approval, Refusal, Result, ResultKind
from harness.budget import Budget
from harness.agent import Agent, build_agent
from harness.call import Call, CallExecution
from harness.checkpoint import Checkpoint
from harness.journal import Journal
from harness.loop import RunResult
from harness.spec import ExperimentSpec, RunContext
from harness.tooling import AgentState
from harness.verification import Verification
from llm.client import LLMClient
from llm.response import LLMOutcome, LLMResponse
from storage.sqlite_store import SQLiteStore
from tools.scenario import Scenario
from tools.tool import TOOL_REGISTRY, Tool, ToolContext


class _Backend:
    name = "fake"


class _Client:
    def __init__(self) -> None:
        self.responses = [
            self._message([{
                "id": "call-1",
                "function": {"name": "submit_diagnosis", "arguments": json.dumps({"root_cause": "no_fault"})},
            }]),
            self._message([]),
        ]

    @staticmethod
    def _message(tool_calls: list[dict]) -> dict:
        return {
            "choices": [{"message": {"role": "assistant", "content": "", "tool_calls": tool_calls}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        }

    def chat(self, messages: list[dict], **kwargs: object) -> dict:
        return self.responses.pop(0)


class DomainObjectTests(unittest.TestCase):
    def test_result_variants_distinguish_approval_and_refusal(self) -> None:
        approval = Result.approval(Approval("run-1", "no_fault", "policy", "approved:run-1", "ok"))
        refusal = Result.refusal(Refusal("BLOCKED: no", "BLOCKED: no"))
        self.assertTrue(approval.ok)
        self.assertIs(approval.kind, ResultKind.APPROVAL)
        self.assertEqual(approval.unwrap().token, "approved:run-1")
        self.assertFalse(refusal.ok)
        self.assertIs(refusal.kind, ResultKind.REFUSAL)
        self.assertEqual(refusal.unwrap().reason, "BLOCKED: no")

    def test_agent_defaults_without_builder(self) -> None:
        agent = Agent()
        self.assertIs(agent.client_factory, LLMClient)
        self.assertIs(agent.tool_registry, TOOL_REGISTRY)
        self.assertEqual(agent, build_agent())

    def test_checkpoint_captures_state_and_run_projection(self) -> None:
        scenario = Scenario.from_dict({"root_cause": "no_fault"})
        state = AgentState(
            scenario=scenario,
            run_id="checkpoint-run",
            approve="policy",
            diagnosis="no_fault",
            diagnosis_submitted=True,
            diagnosis_attempts=1,
            approval_token="approved:checkpoint-run",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteStore(root / "results.sqlite3")
            store.create_run("checkpoint-run", scenario_id=scenario.id, backend="fake", truth=scenario.root_cause)
            budget = Budget.from_snapshot({}, scenario)
            budget.start_step()
            budget.add_tokens(3)
            trace_path = root / "traces" / "checkpoint-run.jsonl"
            checkpoint = Checkpoint(
                run_id="checkpoint-run",
                store=store,
                state=state,
                scenario=scenario,
                budget=budget,
                trace_path=trace_path,
            )
            snapshot = checkpoint.capture()
            saved = store.get_state("checkpoint-run")
            row = store.get_run("checkpoint-run")
            store.close()
        self.assertEqual(snapshot["steps"], 1)
        self.assertEqual(saved["tokens_used"], 3)
        self.assertEqual(snapshot["approval_token"], "approved:checkpoint-run")
        self.assertEqual(row["diagnosis"], "no_fault")
        self.assertTrue(row["approval_granted"])
        self.assertEqual(row["trace_path"], str(trace_path))

    def test_budget_uses_scenario_limits(self) -> None:
        scenario = Scenario.from_dict({"max_steps": 7, "max_tokens": 11, "diagnosis_deadline_steps": 2})
        budget = Budget.from_snapshot({}, scenario)
        self.assertEqual((budget.max_steps, budget.max_tokens, budget.diagnosis_deadline_steps), (7, 11, 2))
        budget.start_step()
        budget.add_tokens(11)
        self.assertEqual(budget.exhausted_reason(), "budget_tokens")

    def test_llm_response_and_call_wrap_wire_shapes(self) -> None:
        response = LLMResponse.from_value({
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            "stream_fallback": True,
        })
        self.assertEqual(response.messages[0].content, "ok")
        self.assertEqual(response.usage.total_tokens, 3)
        self.assertEqual(response.finish_reason, "stop")
        self.assertTrue(response.stream_fallback)
        call = Call.from_tool_call({
            "id": "call-9",
            "function": {"name": "submit_diagnosis", "arguments": '{"root_cause":"no_fault"}'},
        }, 4)
        self.assertEqual(call.id, "call-9")
        self.assertEqual(call.name, "submit_diagnosis")
        self.assertEqual(call.args, {"root_cause": "no_fault"})
        self.assertEqual(call.msg_seq, 4)

    def test_typed_agent_loop_writes_trace_through_store(self) -> None:
        scenario = Scenario.from_dict({
            "id": "control-healthy-noisy",
            "root_cause": "no_fault",
            "acceptable_repairs": [],
            "forbidden_repairs": [],
            "max_steps": 5,
            "max_tokens": 1000,
        })
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteStore(root / "results.sqlite3")
            store.create_run(
                "typed-run",
                scenario_id=scenario.id,
                backend="fake",
                truth=scenario.root_cause,
            )
            spec = ExperimentSpec(
                scenario=scenario,
                backend=_Backend(),
                inject_cmd="",
                verify_cmd="true",
                trace_dir=str(root / "traces"),
                run_env=os.environ.copy(),
            )
            ctx = RunContext(run_id="typed-run", store=store)
            with patch("harness.verification.subprocess.run") as completed:
                completed.return_value.returncode = 0
                completed.return_value.stdout = "ok"
                completed.return_value.stderr = ""
                agent = replace(build_agent(), client_factory=lambda _backend: _Client())
                result: RunResult = agent.run(spec, ctx)
            store.close()
            self.assertIs(result.result.kind, ResultKind.SUCCESS)
            self.assertEqual(result.record["scenario"], scenario.id)
            self.assertEqual(result.record["steps"], 2)
            self.assertTrue(result.record["diagnosis_correct"])
            self.assertTrue(result.verify_ok)
            trace = (root / "traces" / "typed-run.jsonl").read_text(encoding="utf-8")
            self.assertIn("approval", trace)
            self.assertIn("[REDACTED]", trace)
            self.assertNotIn("approved:typed-run", trace)

    def test_response_commits_its_own_audit_trail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteStore(root / "results.sqlite3")
            store.create_run("response-commit", scenario_id="control", backend="fake", truth="no_fault")
            journal = Journal("response-commit", store)
            response = LLMResponse.from_value({
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "accepted approval_token=approved:response-commit",
                        "tool_calls": [{
                            "id": "call-1",
                            "function": {
                                "name": "submit_diagnosis",
                                "arguments": json.dumps({"root_cause": "no_fault", "approval_token": "approved:response-commit"}),
                            },
                        }],
                    },
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            })
            message = response.commit(journal, "fake", 12)
            stored_messages = json.dumps(store.load_messages("response-commit"))
            artifact = store.get_latest_artifact("response-commit", "llm_response_full")
            events = store.load_events("response-commit")
            with store._connect() as conn:
                request = conn.execute("SELECT * FROM llm_requests WHERE run_id=?", ("response-commit",)).fetchone()
            store.close()
        self.assertIn("approved:response-commit", message.content or "")
        self.assertNotIn("approved:response-commit", stored_messages)
        self.assertNotIn("approved:response-commit", artifact["content"])
        self.assertEqual((request["latency_ms"], request["tokens_in"], request["tokens_out"]), (12, 1, 2))
        self.assertTrue(any(event["type"] == "assistant" for event in events))

    def test_llm_outcome_failure_commits_error_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(Path(tmp) / "results.sqlite3")
            store.create_run("llm-fail", scenario_id="control", backend="fake", truth="no_fault")
            journal = Journal("llm-fail", store)
            client = Mock()
            client.chat.side_effect = RuntimeError("boom")
            outcome = LLMOutcome.invoke(client, [], tools=[])
            response = outcome.commit(journal, "fake")
            events = store.load_events("llm-fail")
            artifact = store.get_latest_artifact("llm-fail", "llm_error_full")
            with store._connect() as conn:
                request = conn.execute("SELECT * FROM llm_requests WHERE run_id=?", ("llm-fail",)).fetchone()
            store.close()
        self.assertTrue(outcome.failed)
        self.assertEqual(outcome.end_reason, "error")
        self.assertIsNone(response)
        self.assertTrue(any(event["type"] == "llm_error" for event in events))
        self.assertIn("boom", artifact["content"])
        self.assertIn("boom", request["error"])

    def test_call_manages_intent_execution_and_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(Path(tmp) / "results.sqlite3")
            store.create_run("call-run", scenario_id="control", backend="fake", truth="no_fault")
            journal = Journal("call-run", store)
            tool = Tool(
                name="probe",
                description="probe",
                handler=lambda args: "restarted",
                kind="repair",
                mutating=True,
            )
            call = Call.from_tool_call({
                "id": "call-1",
                "function": {"name": "probe", "arguments": json.dumps({"node": "vm1"})},
            }, 3).for_tool(tool)
            context = ToolContext(call_id="")
            execution = call.invoke(tool, context, journal)
            unresolved = store.has_unknown_mutating_tool_call("call-run")
            tool_message = execution.commit(journal, "fake")
            stored = store.load_tool_calls("call-run")[0]
            messages = store.load_messages("call-run")
            events = store.load_events("call-run")
            store.close()
        self.assertIsInstance(execution, CallExecution)
        self.assertIs(execution.result.kind, ResultKind.SUCCESS)
        self.assertTrue(unresolved)
        self.assertEqual(context.call_id, call.id)
        self.assertEqual(tool_message.content, "restarted")
        self.assertEqual(stored["status"], "finished")
        self.assertEqual(stored["result_raw"], "restarted")
        self.assertEqual(messages[0]["tool_call_id"], "call-1")
        self.assertTrue(any(event["type"] == "tool" for event in events))

    def test_call_failure_is_a_result_variant_and_is_persisted(self) -> None:
        def explode(args: dict[str, Any]) -> str:
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(Path(tmp) / "results.sqlite3")
            store.create_run("call-fail", scenario_id="control", backend="fake", truth="no_fault")
            journal = Journal("call-fail", store)
            tool = Tool(name="explode", description="explode", handler=explode)
            call = Call.from_tool_call({
                "id": "call-fail-1",
                "function": {"name": "explode", "arguments": "{}"},
            }, 1).for_tool(tool)
            execution = call.invoke(tool, ToolContext(call_id=""), journal)
            execution.commit(journal, "fake")
            stored = store.load_tool_calls("call-fail")[0]
            store.close()
        self.assertIs(execution.result.kind, ResultKind.FAILURE)
        self.assertTrue(execution.failed)
        self.assertEqual(stored["status"], "failed")
        self.assertIn("boom", stored["error"])

    def test_verification_commits_and_exposes_result_variant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteStore(root / "results.sqlite3")
            store.create_run("verify-run", scenario_id="control", backend="fake", truth="no_fault")
            journal = Journal("verify-run", store)
            spec = ExperimentSpec(
                scenario=Scenario.from_dict({"root_cause": "no_fault"}),
                backend=_Backend(),
                inject_cmd="",
                verify_cmd="false",
                run_env=os.environ.copy(),
            )
            verification = Verification.run(spec)
            verification.commit(journal)
            artifact = store.get_latest_artifact("verify-run", "verify_output_full")
            events = store.load_events("verify-run")
            store.close()
        self.assertFalse(verification.ok)
        self.assertIs(verification.result.kind, ResultKind.FAILURE)
        self.assertIn("output", artifact["content"])
        self.assertTrue(any(event["type"] == "verify" for event in events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
