from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.scenario_schema import (
    load_scenario,
    scenario_ids,
    validate_scenario,
)


class TestScenarioSchema(unittest.TestCase):
    def test_all_scenarios_validate(self) -> None:
        ids = scenario_ids(ROOT)
        self.assertEqual(len(ids), 6)
        for scenario_id in ids:
            with self.subTest(scenario=scenario_id):
                scenario = load_scenario(ROOT, scenario_id)
                self.assertEqual(validate_scenario(scenario, ROOT, expected_id=scenario_id), [])

    def test_metadata_fields_are_present(self) -> None:
        for scenario_id in scenario_ids(ROOT):
            scenario = load_scenario(ROOT, scenario_id)
            self.assertIn(scenario["risk"], {"read-only", "disruptive", "destructive"})
            self.assertIn(scenario["impact_class"], {"control", "single_member_fault", "quorum_loss"})
            self.assertGreaterEqual(scenario["min_etcd_members"], 1)
            self.assertTrue(scenario["menu"]["label"])
            self.assertTrue(scenario["menu"]["summary"])
            self.assertIn("min_targets", scenario["selector_requirements"])
            self.assertIn("allowed", scenario["selector_requirements"])

    def test_missing_field_is_reported(self) -> None:
        scenario = copy.deepcopy(load_scenario(ROOT, "etcd-member-down"))
        del scenario["risk"]
        errors = validate_scenario(scenario, ROOT, expected_id="etcd-member-down")
        self.assertTrue(any("risk: missing" in error for error in errors))

    def test_disallowed_selector_is_reported(self) -> None:
        scenario = copy.deepcopy(load_scenario(ROOT, "etcd-member-down"))
        scenario["target_nodes"] = ["quorum_losing_set"]
        errors = validate_scenario(scenario, ROOT, expected_id="etcd-member-down")
        self.assertTrue(any("selector kind" in error for error in errors))

    def test_overlapping_repairs_are_reported(self) -> None:
        scenario = copy.deepcopy(load_scenario(ROOT, "etcd-member-down"))
        scenario["forbidden_repairs"] = ["restart_member"]
        errors = validate_scenario(scenario, ROOT, expected_id="etcd-member-down")
        self.assertTrue(any("overlap" in error for error in errors))


if __name__ == "__main__":
    unittest.main(verbosity=2)
