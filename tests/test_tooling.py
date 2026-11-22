#!/usr/bin/env python3
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.vocab import DESTRUCTIVE_TOOLS, DIAGNOSIS_TOOL, RESTART_MEMBER
from harness.tooling import AgentState, Diagnosis, Repair
from tools.tool import Tool, ToolContext, ToolOutcome, build_tool_registry


class TestTooling(unittest.TestCase):
    def test_tool_renders_schema_and_runs_handler(self) -> None:
        tool = Tool("probe", "Probe the cluster.", {"node": {"type": "string"}}, ("node",),
                    lambda args: f"probed {args['node']}")
        schema = tool.schema()
        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["function"]["name"], "probe")
        self.assertEqual(schema["function"]["parameters"]["required"], ["node"])
        self.assertEqual(json.loads(tool.to_string()), schema)
        self.assertEqual(tool.run({"node": "node-a"}).message, "probed node-a")

    def test_registry_exposes_diagnosis_and_repair_metadata(self) -> None:
        registry = build_tool_registry()
        diagnosis = registry.get(DIAGNOSIS_TOOL)
        repair = registry.get(RESTART_MEMBER)
        self.assertIsNotNone(diagnosis)
        self.assertIsNotNone(repair)
        self.assertEqual(diagnosis.kind, "diagnosis")
        self.assertTrue(repair.mutating)
        self.assertFalse(repair.destructive)
        self.assertEqual({tool.name for tool in registry if tool.destructive}, DESTRUCTIVE_TOOLS & {tool.name for tool in registry})
        self.assertEqual(len(registry.schemas()), len(registry.tools))
        self.assertEqual(len(registry.to_strings()), len(registry.tools))

    def test_diagnosis_execute_returns_outcome(self) -> None:
        scenario = {"root_cause": "etcd_member_unreachable"}
        state = AgentState(scenario, "run-1", "policy")
        result = Diagnosis(state).execute(Tool(DIAGNOSIS_TOOL, ""), {"root_cause": "etcd_member_unreachable"}, "call-1")
        self.assertIsInstance(result, ToolOutcome)
        self.assertTrue(result.diagnosis_submitted)
        self.assertTrue(result.diagnosis_correct)
        self.assertEqual(result.approval_token, "approved:run-1")

    def test_repair_execute_blocks_without_diagnosis(self) -> None:
        scenario = {"acceptable_repairs": [RESTART_MEMBER], "forbidden_repairs": []}
        state = AgentState(scenario, "run-1", "policy")
        tool = Tool(RESTART_MEMBER, "", kind="repair", mutating=True)
        result = Repair(state, build_tool_registry()).execute(tool, {"node": "node-a"}, "call-1")
        self.assertEqual(result.status, "denied")
        self.assertIn("diagnosis required first", result.message)


if __name__ == "__main__":
    unittest.main()
