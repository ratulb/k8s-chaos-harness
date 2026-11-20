from __future__ import annotations

import socket
import subprocess
from typing import Tuple


def current_node() -> str:
    return socket.gethostname().split(".")[0]


def node_ip(node: str) -> str:
    out = subprocess.run(
        ["kubectl", "get", "node", node, "-o", "jsonpath={.status.addresses[?(@.type==\"InternalIP\")].address}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def is_local(node: str) -> bool:
    return node == current_node()


def run_on_node(node: str, script: str, timeout: int = 120) -> Tuple[int, str, str]:
    if is_local(node):
        proc = subprocess.run(["bash"], input=script, capture_output=True, text=True, timeout=timeout)
    else:
        ip = node_ip(node)
        proc = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10", ip, "bash"],
            input=script,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    return proc.returncode, proc.stdout, proc.stderr
