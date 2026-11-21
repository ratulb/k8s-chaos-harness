#!/usr/bin/env python3
"""Unit tests for portable cluster discovery.

The environment variables are patched in-process so these tests work on any
machine and never depend on a live Kubernetes cluster or specific hostnames.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import cluster_config


class TestClusterConfig(unittest.TestCase):
    def setUp(self):
        # nodes() is cached; clear it so each test can install its own env.
        cluster_config.nodes.cache_clear()

    def tearDown(self):
        # Avoid leaking synthetic node names into later tests in the same run.
        cluster_config.nodes.cache_clear()

    def test_env_overrides_discovery(self):
        # K8S_NODES must take precedence over kubectl discovery.
        names = ["node-a", "node-b", "node-c"]
        with patch.dict(os.environ, {"K8S_NODES": ",".join(names)}):
            self.assertEqual(cluster_config.nodes(), names)

    def test_roles_resolve_without_hardcoded_names(self):
        # Abstract roles and numeric positions must resolve from whatever node
        # list the operator supplied, not from fixed hostname conventions.
        names = ["node-a", "node-b", "node-c"]
        with patch.dict(os.environ, {"K8S_NODES": ",".join(names)}):
            self.assertEqual(cluster_config.node_by_role("first"), "node-a")
            self.assertEqual(cluster_config.node_by_role("second"), "node-b")
            self.assertEqual(cluster_config.node_by_role("third"), "node-c")
            self.assertEqual(cluster_config.node_by_role("2"), "node-b")
            self.assertEqual(cluster_config.node_by_role("node-c"), "node-c")

    def test_ssh_user_override(self):
        # SSH_USER may be empty (use current account) or explicitly overridden;
        # neither case should bake in an Azure-specific login name.
        with patch.dict(os.environ, {"SSH_USER": ""}):
            self.assertEqual(cluster_config.ssh_user(), cluster_config.getpass.getuser())
        with patch.dict(os.environ, {"SSH_USER": "operator"}):
            self.assertEqual(cluster_config.ssh_user(), "operator")

    def test_shell_env_contract(self):
        # Bash scripts eval this output, so it must define NODES and role
        # variables with shell-safe quoting.
        names = ["node-a", "node-b", "node-c"]
        with patch.dict(os.environ, {"K8S_NODES": ",".join(names), "SSH_USER": "operator"}):
            out = cluster_config.shell_env()
        self.assertIn('NODES=(node-a node-b node-c)', out)
        self.assertIn('FIRST_NODE=node-a', out)
        self.assertIn('SECOND_NODE=node-b', out)
        self.assertIn('THIRD_NODE=node-c', out)
        self.assertIn('SSH_USER=operator', out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
