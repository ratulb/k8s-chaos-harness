from __future__ import annotations

import re
import shlex
import subprocess

from .truncate import truncate
from .ssh import run_on_node
from . import cluster_config


ETCD_CERT = "/etc/kubernetes/pki/etcd"
_UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._:-]*$")
_CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_PATH_PREFIXES = (
    "/var/lib/etcd",
    "/var/lib/kubelet",
    "/etc/kubernetes/manifests",
    "/var/log",
    "/tmp",
)


def _validate_node(node: str) -> str:
    if node not in cluster_config.nodes():
        return f"invalid node {node!r}"
    return ""


def _validate_unit(unit: str) -> str:
    if not unit or not _UNIT_RE.fullmatch(unit):
        return f"invalid unit {unit!r}"
    return ""


def _validate_container(container_id: str) -> str:
    if not container_id or not _CONTAINER_RE.fullmatch(container_id):
        return f"invalid container id {container_id!r}"
    return ""


def _validate_path(path: str) -> str:
    if not path.startswith("/") or any(ch.isspace() for ch in path) or ".." in path:
        return f"invalid path {path!r}"
    if not any(path == prefix or path.startswith(prefix + "/") for prefix in _PATH_PREFIXES):
        return f"path not allowed {path!r}"
    return ""


def _validate_tail(value: int) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 1000:
        return f"invalid line count {value!r}"
    return ""


def _etcdctl(node: str, args: list[str]) -> str:
    error = _validate_node(node)
    if error:
        return error
    pod = f"etcd-{node}"
    cmd = ["kubectl", "exec", "-n", "kube-system", pod, "--", "etcdctl",
           "--endpoints=https://127.0.0.1:2379",
           f"--cacert={ETCD_CERT}/ca.crt",
           f"--cert={ETCD_CERT}/server.crt",
           f"--key={ETCD_CERT}/server.key"] + args
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    out = proc.stdout + (("\nSTDERR:\n" + proc.stderr) if proc.stderr else "")
    if proc.returncode != 0:
        out += f"\n(exit {proc.returncode})"
    return truncate(out)


def etcdctl_endpoint_health(endpoints: str = "") -> str:
    return _etcdctl(cluster_config.default_node(), ["endpoint", "health", "--cluster"])


def etcdctl_endpoint_status(node: str = "") -> str:
    return _etcdctl(node or cluster_config.default_node(), ["endpoint", "status", "-w", "table"])


def etcdctl_member_list(node: str = "") -> str:
    return _etcdctl(node or cluster_config.default_node(), ["member", "list", "-w", "table"])


def etcdctl_alarm_list(node: str = "") -> str:
    return _etcdctl(node or cluster_config.default_node(), ["alarm", "list"])


def journalctl_tail(node: str, unit: str, lines: int = 100) -> str:
    for error in (_validate_node(node), _validate_unit(unit), _validate_tail(lines)):
        if error:
            return error
    rc, out, err = run_on_node(
        node,
        f"journalctl -u {shlex.quote(unit)} -n {shlex.quote(str(lines))} --no-pager",
    )
    return truncate(out + (("\nSTDERR:\n" + err) if err else ""))


def crictl_ps(node: str) -> str:
    rc, out, err = run_on_node(node, "sudo crictl ps 2>/dev/null || sudo ctr -n k8s.io containers list")
    return truncate(out + (("\nSTDERR:\n" + err) if err else ""))


def crictl_logs(node: str, container_id: str, tail: int = 100) -> str:
    for error in (_validate_node(node), _validate_container(container_id), _validate_tail(tail)):
        if error:
            return error
    rc, out, err = run_on_node(
        node,
        f"sudo crictl logs --tail={shlex.quote(str(tail))} {shlex.quote(container_id)} 2>/dev/null || echo 'crictl unavailable'",
    )
    return truncate(out + (("\nSTDERR:\n" + err) if err else ""))


def systemctl_status(node: str, unit: str) -> str:
    for error in (_validate_node(node), _validate_unit(unit)):
        if error:
            return error
    rc, out, err = run_on_node(node, f"systemctl status {shlex.quote(unit)} --no-pager -l")
    return truncate(out + (("\nSTDERR:\n" + err) if err else ""))


def df_path(node: str, path: str) -> str:
    for error in (_validate_node(node), _validate_path(path)):
        if error:
            return error
    rc, out, err = run_on_node(node, f"df -h {shlex.quote(path)}")
    return truncate(out + (("\nSTDERR:\n" + err) if err else ""))


def ls_path(node: str, path: str) -> str:
    for error in (_validate_node(node), _validate_path(path)):
        if error:
            return error
    rc, out, err = run_on_node(node, f"ls -la {shlex.quote(path)}")
    return truncate(out + (("\nSTDERR:\n" + err) if err else ""))
