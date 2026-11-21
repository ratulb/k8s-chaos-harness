"""Portable cluster discovery and SSH helpers.

No machine names, IP addresses, or login users are hardcoded here. Operators can
pin values with K8S_NODES / SSH_USER / SSH_KEY; otherwise node names come from
Kubernetes and the SSH user defaults to the account running the harness.
"""
from __future__ import annotations

import getpass
import json
import os
import shlex
import socket
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from tools import topology

ROLES = topology.ORDINAL_ROLES
CACHE_PATH = Path.home() / ".cache" / "k8s-chaos-harness" / "node_ips.json"


def _installer_conf() -> dict[str, list[str]]:
    """Read hosts from k8s-easy-install/setup.conf when Kubernetes is down."""
    install_dir = Path(os.environ.get("K8S_EASY_INSTALL", Path.home() / "k8s-easy-install"))
    path = install_dir / "setup.conf"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {}

    result: dict[str, list[str]] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        try:
            result[key.strip()] = shlex.split(value, comments=True)
        except ValueError:
            result[key.strip()] = value.split()
    return result


@lru_cache(maxsize=1)
def _installer_node_ips() -> dict[str, str]:
    """Map installer host addresses to node hostnames via SSH."""
    mapping: dict[str, str] = {}
    for ip in _installer_conf().get("masters", []):
        try:
            reverse = socket.gethostbyaddr(ip)[0].split(".")[0]
        except Exception:
            reverse = ""
        if reverse:
            mapping[reverse] = ip
            continue

        target = f"{ssh_user()}@{ip}"
        proc = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
             target, "hostname -s"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            mapping[proc.stdout.strip()] = ip
    return mapping


@lru_cache(maxsize=1)
def nodes() -> list[str]:
    """Return control-plane node names from frozen topology, config, or discovery."""
    frozen = topology.frozen_topology()
    if frozen:
        return list(frozen["control_planes"])

    configured = os.environ.get("K8S_NODES", "").strip()
    if configured:
        return [name for name in configured.replace(",", " ").split() if name]

    return list(topology.discover_topology()["control_planes"])


def node_by_role(role: str) -> str:
    """Resolve an abstract role such as 'second' or an explicit cluster node name."""
    all_nodes = nodes()
    if role in all_nodes:
        return role
    index = topology.ordinal_index(role)
    if index is not None:
        if index <= len(all_nodes):
            return all_nodes[index - 1]
        raise RuntimeError(f"node role {role!r} exceeds {len(all_nodes)} discovered node(s)")
    raise RuntimeError(f"unknown node role {role!r}; expected one of {all_nodes} or {ROLES}")


def default_node() -> str:
    """Return a stable probe node; operators may override with PROBE_NODE."""
    frozen = topology.frozen_topology()
    if frozen and not os.environ.get("PROBE_NODE", "").strip():
        return frozen["probe_node"]
    override = os.environ.get("PROBE_NODE", "").strip()
    return node_by_role(override) if override else node_by_role("first")


def current_node() -> str:
    """Short hostname of the machine running this process."""
    return socket.gethostname().split(".")[0]


def ssh_user() -> str:
    """SSH login name; defaults to the current OS account, never hardcoded."""
    return os.environ.get("SSH_USER", "").strip() or getpass.getuser()


def ssh_key() -> str:
    return os.environ.get("SSH_KEY", "").strip()


def _env_ip_map() -> dict[str, str]:
    mapping = {}
    for part in os.environ.get("K8S_NODE_IPS", "").split(","):
        if "=" in part:
            name, ip = part.split("=", 1)
            mapping[name.strip()] = ip.strip()
    return mapping


def _cached_ip_map() -> dict[str, str]:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def cache_node_ips() -> None:
    """Persist currently discovered IPs for use when the API is unavailable."""
    mapping = {name: node_ip(name) for name in nodes()}
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(mapping, indent=2) + "\n", encoding="utf-8")


def clear_ip_cache(path: Path | None = None) -> bool:
    target = path or CACHE_PATH
    removed = False
    try:
        target.unlink()
        removed = True
    except FileNotFoundError:
        pass
    nodes.cache_clear()
    _installer_node_ips.cache_clear()
    return removed


def node_ip(node: str) -> str:
    """Resolve a node's InternalIP via Kubernetes, env, or a saved cache."""
    try:
        resolved = node_by_role(node)
    except RuntimeError:
        # During a fault kubectl discovery may fail; an explicit node name is
        # still usable with env/cache lookups.
        resolved = node

    frozen = topology.frozen_topology()
    if frozen:
        for detail in frozen.get("node_details", []):
            if detail.get("name") == resolved and detail.get("ip"):
                return detail["ip"]

    overrides = {**_installer_node_ips(), **_cached_ip_map(), **_env_ip_map()}
    if resolved in overrides:
        return overrides[resolved]

    proc = subprocess.run(
        ["kubectl", "get", "node", resolved,
         "-o", "jsonpath={.status.addresses[?(@.type==\"InternalIP\")].address}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    raise RuntimeError(f"cannot resolve IP for node {resolved}: {proc.stderr.strip()}")


def ssh_target(node: str) -> str:
    """Build user@host target for a node, or an empty string when local."""
    resolved = node_by_role(node)
    if resolved == current_node():
        return ""
    return f"{ssh_user()}@{node_ip(resolved)}"


def ssh_cmd(node: str) -> list[str]:
    """SSH command prefix for remote execution on a node."""
    target = ssh_target(node)
    cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
    key = ssh_key()
    if key:
        cmd += ["-i", key]
    return cmd + [target, "bash"]


def shell_env() -> str:
    """Return shell assignments for bash scripts that need node metadata."""
    frozen = topology.frozen_topology()
    all_nodes = list(frozen["all_nodes"]) if frozen else nodes()
    control_planes = list(frozen["control_planes"]) if frozen else nodes()
    etcd_members = [member["name"] for member in frozen["etcd_members"]] if frozen else control_planes
    quorum_loss_nodes = list(frozen["quorum_loss_nodes"]) if frozen else []
    member_count = len(etcd_members)
    values = [f"NODES=({' '.join(shlex.quote(n) for n in control_planes)})"]
    values.append(f"ALL_NODES=({' '.join(shlex.quote(n) for n in all_nodes)})")
    values.append(f"CONTROL_PLANES=({' '.join(shlex.quote(n) for n in control_planes)})")
    values.append(f"ETCD_MEMBERS=({' '.join(shlex.quote(n) for n in etcd_members)})")
    values.append(f"QUORUM_LOSS_NODES=({' '.join(shlex.quote(n) for n in quorum_loss_nodes)})")
    values.append(f"NODE_COUNT={len(all_nodes)}")
    values.append(f"CONTROL_PLANE_COUNT={len(control_planes)}")
    values.append(f"ETCD_MEMBER_COUNT={member_count}")
    values.append(f"QUORUM={topology.quorum_for(member_count) if member_count else 0}")
    values.append(f"QUORUM_LOSS_TARGETS={topology.quorum_loss_target_count(member_count)}")
    values.append(f"SSH_USER={shlex.quote(ssh_user())}")
    values.append(f"SSH_KEY={shlex.quote(ssh_key())}")
    for idx, node in enumerate(all_nodes):
        role = ROLES[idx] if idx < len(ROLES) else topology.ordinal_name(idx + 1)
        values.append(f"{role.upper()}_NODE={shlex.quote(node)}")
        values.append(f"NODE_{idx + 1}={shlex.quote(node)}")
    values.append(f"PROBE_NODE={shlex.quote(default_node())}")
    return "\n".join(values) + "\n"


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: cluster_config.py nodes|ip NODE|ssh-target NODE|ROLE|probe|env|cache-ips|clear-cache")
    cmd = sys.argv[1]
    if cmd == "nodes":
        print("\n".join(nodes()))
    elif cmd == "ip":
        print(node_ip(sys.argv[2]))
    elif cmd == "ssh-target":
        print(ssh_target(sys.argv[2]))
    elif cmd in ROLES:
        print(node_by_role(cmd))
    elif cmd == "probe":
        print(default_node())
    elif cmd == "env":
        print(shell_env(), end="")
    elif cmd == "cache-ips":
        cache_node_ips()
        print(f"cached node IPs in {CACHE_PATH}")
    elif cmd == "clear-cache":
        removed = clear_ip_cache()
        print(f"cleared node IP cache {CACHE_PATH}" if removed else f"no node IP cache at {CACHE_PATH}")
    else:
        raise SystemExit(f"unknown command: {cmd}")


if __name__ == "__main__":
    main()
