from __future__ import annotations

import socket
import subprocess
import time
from typing import Tuple


def current_node() -> str:
    return socket.gethostname().split(".")[0]


NODE_IPS = {"vm1": "10.1.0.4", "vm2": "10.2.0.4", "vm3": "10.3.0.4"}


def node_ip(node: str) -> str:
    for _ in range(3):
        out = subprocess.run(
            ["kubectl", "get", "node", node, "-o", "jsonpath={.status.addresses[?(@.type==\"InternalIP\")].address}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
        time.sleep(2)
    if node in NODE_IPS:
        return NODE_IPS[node]
    raise RuntimeError(f"cannot resolve IP for node {node}")


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
