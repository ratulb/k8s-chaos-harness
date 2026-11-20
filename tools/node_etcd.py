from __future__ import annotations

import subprocess

from .truncate import truncate
from .ssh import run_on_node


ETCD_CERT = "/etc/kubernetes/pki/etcd"


def _etcdctl(node: str, args: list[str]) -> str:
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
    return _etcdctl("vm1", ["endpoint", "health", "--cluster"])


def etcdctl_endpoint_status(node: str = "vm1") -> str:
    return _etcdctl(node, ["endpoint", "status", "-w", "table"])


def etcdctl_member_list(node: str = "vm1") -> str:
    return _etcdctl(node, ["member", "list", "-w", "table"])


def etcdctl_alarm_list(node: str = "vm1") -> str:
    return _etcdctl(node, ["alarm", "list"])


def journalctl_tail(node: str, unit: str, lines: int = 100) -> str:
    rc, out, err = run_on_node(node, f"journalctl -u {unit} -n {lines} --no-pager")
    return truncate(out + (("\nSTDERR:\n" + err) if err else ""))


def crictl_ps(node: str) -> str:
    rc, out, err = run_on_node(node, "sudo crictl ps 2>/dev/null || sudo ctr -n k8s.io containers list")
    return truncate(out + (("\nSTDERR:\n" + err) if err else ""))


def crictl_logs(node: str, container_id: str, tail: int = 100) -> str:
    rc, out, err = run_on_node(node, f"sudo crictl logs --tail={tail} {container_id} 2>/dev/null || echo 'crictl unavailable'")
    return truncate(out + (("\nSTDERR:\n" + err) if err else ""))


def systemctl_status(node: str, unit: str) -> str:
    rc, out, err = run_on_node(node, f"systemctl status {unit} --no-pager -l")
    return truncate(out + (("\nSTDERR:\n" + err) if err else ""))


def df_path(node: str, path: str) -> str:
    rc, out, err = run_on_node(node, f"df -h {path}")
    return truncate(out + (("\nSTDERR:\n" + err) if err else ""))


def ls_path(node: str, path: str) -> str:
    rc, out, err = run_on_node(node, f"ls -la {path}")
    return truncate(out + (("\nSTDERR:\n" + err) if err else ""))
