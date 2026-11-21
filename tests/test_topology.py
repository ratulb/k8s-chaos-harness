from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import cluster_config
from tools import topology
from tools.scenario_schema import load_scenario


class TestTopology(unittest.TestCase):
    def setUp(self) -> None:
        topology.clear_frozen_topology()

    def tearDown(self) -> None:
        topology.clear_frozen_topology()

    def _fixture(self, name: str) -> Path:
        return ROOT / "tests" / "fixtures" / "topologies" / f"{name}.json"

    def _discover(self, name: str) -> dict:
        with patch.dict(os.environ, {"CHAOS_TOPOLOGY_JSON": str(self._fixture(name))}):
            return topology.discover_topology()

    def test_quorum_math(self) -> None:
        expected = {
            1: (1, 1),
            2: (2, 1),
            3: (2, 2),
            4: (3, 2),
            5: (3, 3),
            7: (4, 4),
        }
        for members, (quorum, targets) in expected.items():
            self.assertEqual(topology.quorum_for(members), quorum)
            self.assertEqual(topology.quorum_loss_target_count(members), targets)

    def test_fixture_topologies(self) -> None:
        expected = {
            "n1": (1, 1, 1),
            "n2": (2, 2, 1),
            "n3": (3, 2, 2),
            "n4": (4, 3, 2),
            "n5": (5, 3, 3),
            "n7": (7, 4, 4),
        }
        for name, (members, quorum, targets) in expected.items():
            discovered = self._discover(name)
            self.assertEqual(discovered["etcd_member_count"], members)
            self.assertEqual(discovered["quorum"], quorum)
            self.assertEqual(discovered["quorum_loss_targets"], targets)
            self.assertEqual(len(discovered["quorum_loss_nodes"]), targets)
            self.assertEqual(discovered["control_planes"], [f"node-{index}" for index in range(1, members + 1)])

    def test_topology_hash_is_stable_and_ignores_health(self) -> None:
        def payload(ready: bool) -> str:
            return json.dumps({
                "nodes": ["node-1", "node-2", "node-3"],
                "etcd_members": ["node-1", "node-2", "node-3"],
                "node_details": [
                    {"name": "node-1", "ip": "10.0.0.1", "ready": ready},
                    {"name": "node-2", "ip": "10.0.0.2", "ready": ready},
                    {"name": "node-3", "ip": "10.0.0.3", "ready": ready},
                ],
            })

        with patch.dict(os.environ, {"CHAOS_TOPOLOGY_JSON": payload(True)}):
            healthy = topology.discover_topology()
        with patch.dict(os.environ, {"CHAOS_TOPOLOGY_JSON": payload(False)}):
            unhealthy = topology.discover_topology()
        self.assertEqual(healthy["topology_hash"], unhealthy["topology_hash"])

    def test_resolve_targets(self) -> None:
        discovered = self._discover("n5")
        resolved = topology.resolve_targets(
            discovered,
            ["first", "3", "quorum_losing_set", "node-2"],
        )
        self.assertEqual(resolved, ["node-1", "node-3", "node-4", "node-5", "node-2"])
        with self.assertRaises(RuntimeError):
            topology.resolve_targets(discovered, ["ninth"])

    def test_capability_single_member_quorum_loss(self) -> None:
        discovered = self._discover("n1")
        scenario = load_scenario(ROOT, "etcd-quorum-loss")
        benchmark = topology.capability_check(scenario, discovered, "benchmark")
        self.assertFalse(benchmark["eligible"])
        self.assertIn("total outage", benchmark["unsupported_reason"])
        repair = topology.capability_check(scenario, discovered, "repair")
        self.assertTrue(repair["eligible"])
        self.assertEqual(repair["impact_class"], "total_outage")
        self.assertTrue(any("best-effort" in item for item in repair["warning"]))

    def test_capability_warns_on_even_and_small_topology(self) -> None:
        discovered = self._discover("n2")
        scenario = load_scenario(ROOT, "etcd-member-down")
        result = topology.capability_check(scenario, discovered, "benchmark")
        self.assertTrue(result["eligible"])
        joined = " ".join(result["warning"])
        self.assertIn("even etcd member count", joined)
        self.assertIn("3 is the loud recommendation", joined)

    def test_frozen_topology_drives_cluster_config(self) -> None:
        discovered = self._discover("n4")
        topology.freeze_topology(discovered)
        self.assertEqual(cluster_config.nodes(), [f"node-{index}" for index in range(1, 5)])
        self.assertEqual(cluster_config.node_by_role("fourth"), "node-4")
        env = cluster_config.shell_env()
        self.assertIn("NODE_COUNT=4", env)
        self.assertIn("ETCD_MEMBER_COUNT=4", env)
        self.assertIn("QUORUM=3", env)
        self.assertIn("QUORUM_LOSS_TARGETS=2", env)
        self.assertIn("NODES=(node-1 node-2 node-3 node-4)", env)


if __name__ == "__main__":
    unittest.main(verbosity=2)
