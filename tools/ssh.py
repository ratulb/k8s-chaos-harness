"""Remote/exec helpers built on portable cluster configuration."""
from __future__ import annotations

import subprocess
from typing import Tuple

from . import cluster_config


def current_node() -> str:
    return cluster_config.current_node()


def node_ip(node: str) -> str:
    return cluster_config.node_ip(node)


def is_local(node: str) -> bool:
    return cluster_config.node_by_role(node) == current_node()


def run_on_node(node: str, script: str, timeout: int = 120) -> Tuple[int, str, str]:
    """Run a bash script locally when node is this host, otherwise over SSH."""
    if is_local(node):
        proc = subprocess.run(["bash"], input=script, capture_output=True, text=True, timeout=timeout)
    else:
        proc = subprocess.run(cluster_config.ssh_cmd(node), input=script, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr
