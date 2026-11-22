from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any

from tools import cluster_config, topology as topology_tools
from tools.scenario import as_scenario
from tools.ssh import run_on_node

FAULT_TIMEOUTS = {
    "control_healthy_noisy": 0.0,
    "etcd-disk-full": 15.0,
    "etcd_disk_full": 15.0,
    "etcd-member-down": 60.0,
    "etcd_member_down": 60.0,
    "etcd-member-wiped": 60.0,
    "etcd_member_wiped": 60.0,
    "etcd-wrong-peer-url": 60.0,
    "etcd_wrong_peer_url": 60.0,
    "etcd-quorum-loss": 60.0,
    "etcd_quorum_loss": 60.0,
}
POLL_INTERVAL = 1.0
ETCDCTL_ARGS = [
    "--endpoints=https://127.0.0.1:2379",
    "--cacert=/etc/kubernetes/pki/etcd/ca.crt",
    "--cert=/etc/kubernetes/pki/etcd/server.crt",
    "--key=/etc/kubernetes/pki/etcd/server.key",
]


def timeout_for(scenario: Any) -> float:
    value = as_scenario(scenario)
    configured = value.fault_present_timeout_seconds
    if configured is not None:
        return float(configured)
    return FAULT_TIMEOUTS.get(value.id, 60.0)


def _kubectl(args: list[str], timeout: int = 20) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["kubectl", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except Exception as exc:
        return False, str(exc)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def _pod_ready(node: str) -> bool:
    ok, output = _kubectl([
        "--request-timeout=5s", "get", "pod", f"etcd-{node}", "-n", "kube-system",
        "-o", "jsonpath={.status.containerStatuses[0].ready}",
    ])
    return ok and output.strip() == "true"


def _probe_node(target: str) -> str:
    preferred = ""
    try:
        preferred = cluster_config.default_node()
    except Exception:
        preferred = ""
    if preferred and preferred != target:
        return preferred
    for node in cluster_config.nodes():
        if node != target:
            return node
    return target


def _members(probe: str) -> list[dict[str, Any]]:
    ok, output = _kubectl([
        "--request-timeout=10s", "exec", "-n", "kube-system", f"etcd-{probe}",
        "--", "etcdctl", *ETCDCTL_ARGS, "--write-out=json", "member", "list",
    ])
    if not ok:
        ok, output = _kubectl([
            "--request-timeout=10s", "exec", "-n", "kube-system", f"etcd-{probe}",
            "--", "etcdctl", *ETCDCTL_ARGS, "member", "list",
        ])
    if not ok:
        return []
    try:
        payload = json.loads(output)
    except Exception:
        return []
    members = payload.get("members") if isinstance(payload, dict) else None
    return [item for item in (members or []) if isinstance(item, dict)]


def _peer_url(target: str) -> tuple[bool, str]:
    probe = _probe_node(target)
    try:
        address = cluster_config.node_ip(target)
    except Exception as exc:
        return False, f"cannot resolve target address: {exc}"
    for member in _members(probe):
        peer = str(member.get("peerURLs") or member.get("peerUrls") or "")
        if isinstance(member.get("peerURLs"), list):
            peer = " ".join(str(item) for item in member["peerURLs"])
        names = " ".join(str(member.get(key, "")) for key in ("name", "node", "clientURLs"))
        if target in names or address in peer or address in names:
            return True, peer
    return False, f"member for {target} ({address}) not found"


def _disk_full(target: str) -> tuple[bool, str]:
    script = (
        "sudo test -f /var/lib/etcd/filler; echo FILLER:$?; "
        "sudo df -Pk /var/lib/etcd 2>/dev/null | awk 'NR==2{gsub(/%/,\"\",$5); print $5}'"
    )
    try:
        code, out, err = run_on_node(target, script, timeout=30)
    except Exception as exc:
        return False, f"disk probe failed: {exc}"
    text = (out + "\n" + err).strip()
    if "FILLER:0" in text:
        return True, text
    for line in out.splitlines():
        value = line.strip()
        if value.isdigit() and int(value) >= 95:
            return True, text
    return False, text or f"disk probe exited {code}"


def _wiped(target: str) -> tuple[bool, str]:
    script = (
        "sudo test ! -f /var/lib/etcd/member/snap/db; echo DB_MISSING:$?; "
        "sudo grep -c -- '--version' /etc/kubernetes/manifests/etcd.yaml || true"
    )
    try:
        code, out, err = run_on_node(target, script, timeout=30)
    except Exception as exc:
        return False, f"wipe probe failed: {exc}"
    text = (out + "\n" + err).strip()
    if not _pod_ready(target):
        if "DB_MISSING:0" in text:
            return True, text
        lines = out.strip().splitlines()
        count = lines[-1] if lines else "0"
        if count.isdigit() and int(count) > 0:
            return True, text
    return False, text or f"wipe probe exited {code}"


def _quorum_lost(targets: list[str]) -> tuple[bool, str]:
    frozen = topology_tools.frozen_topology()
    nodes = list(frozen["control_planes"]) if frozen else list(cluster_config.nodes())
    if not nodes:
        nodes = list(targets)
    size = len(nodes) or len(targets) or 1
    quorum = topology_tools.quorum_for(size)
    ready = sum(1 for node in nodes if _pod_ready(node))
    if ready < quorum:
        return True, f"etcd pods ready {ready}/{size}, quorum {quorum}"
    return False, f"etcd pods ready {ready}/{size}, quorum {quorum}"


def check_fault_present(
    scenario: Any,
    targets: list[str],
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    scenario_id = as_scenario(scenario).id
    check_env = os.environ if env is None else env
    if check_env.get("CHAOS_FAULT_CHECK_MODE") == "fixture":
        return True, "fixture mode skips live fault check"
    target = targets[0] if targets else ""
    if scenario_id in {"control-healthy-noisy", "control_healthy_noisy"}:
        return True, "control scenario requires no fault stimulus"
    if not target and scenario_id != "etcd-quorum-loss":
        return False, "no target node resolved"
    try:
        if scenario_id in {"etcd-member-down", "etcd_member_down"}:
            if not _pod_ready(target):
                return True, f"etcd pod {target} is not ready"
            return False, f"etcd pod {target} is still ready"
        if scenario_id in {"etcd-disk-full", "etcd_disk_full"}:
            return _disk_full(target)
        if scenario_id in {"etcd-wrong-peer-url", "etcd_wrong_peer_url"}:
            found, peer = _peer_url(target)
            if not found:
                return False, peer
            if ":2399" in peer:
                return True, f"registered peer URL is {peer}"
            return False, f"registered peer URL is still {peer}"
        if scenario_id in {"etcd-member-wiped", "etcd_member_wiped"}:
            return _wiped(target)
        if scenario_id in {"etcd-quorum-loss", "etcd_quorum_loss"}:
            return _quorum_lost(targets or cluster_config.nodes())
    except Exception as exc:
        return False, f"fault check failed: {exc}"
    return False, f"no fault predicate for scenario {scenario_id!r}"


def wait_for_fault(
    scenario: Any,
    targets: list[str],
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> tuple[bool, str, float]:
    limit = timeout_for(scenario) if timeout is None else float(timeout)
    if limit <= 0:
        present, detail = check_fault_present(scenario, targets, env)
        return present, detail, 0.0
    started = time.monotonic()
    while True:
        present, detail = check_fault_present(scenario, targets, env)
        elapsed = time.monotonic() - started
        if present:
            return True, detail, elapsed
        if elapsed >= limit:
            return False, detail, elapsed
        time.sleep(min(POLL_INTERVAL, limit - elapsed))
