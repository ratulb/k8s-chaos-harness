from __future__ import annotations

import subprocess

from .truncate import truncate


def _run(cmd: list[str], timeout: int = 60) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = proc.stdout
    if proc.stderr:
        out += "\nSTDERR:\n" + proc.stderr
    if proc.returncode != 0:
        out += f"\n(exit {proc.returncode})"
    return truncate(out)


def kubectl_get(resource: str, namespace: str = "") -> str:
    cmd = ["kubectl", "get", resource]
    if namespace:
        cmd += ["-n", namespace]
    cmd += ["-o", "wide"]
    return _run(cmd)


def kubectl_describe(resource: str, name: str, namespace: str = "") -> str:
    cmd = ["kubectl", "describe", resource, name]
    if namespace:
        cmd += ["-n", namespace]
    return _run(cmd)


def kubectl_logs(pod: str, container: str = "", tail_lines: int = 100, namespace: str = "default") -> str:
    cmd = ["kubectl", "logs", pod, "-n", namespace, f"--tail={tail_lines}"]
    if container:
        cmd += ["-c", container]
    return _run(cmd)


def kubectl_events(namespace: str = "default") -> str:
    return _run(["kubectl", "get", "events", "-n", namespace, "--sort-by=.lastTimestamp"])
