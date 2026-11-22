from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.results import Approval, Refusal, Result, ResultKind
from harness.budget import Budget
from harness.call import Call
from harness.agent import Agent, build_agent
from harness.checkpoint import Checkpoint
from harness.loop import RunResult
from harness.spec import ExperimentSpec, RunContext
from harness.tooling import AgentState
from llm.client import LLMClient
from llm.response import LLMResponse
from storage.sqlite_store import SQLiteStore
from tools.scenario import Scenario
from tools.tool import TOOL_REGISTRY


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
            with patch("harness.loop.subprocess.run") as completed:
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
