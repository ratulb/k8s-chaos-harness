from __future__ import annotations

import subprocess
import time

from pathlib import Path

from . import cluster_config
from .ssh import run_on_node, node_ip


def _kubectl(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _wait_for_etcd(node: str, timeout: int = 180) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        proc = _kubectl(["kubectl", "get", "pod", f"etcd-{node}", "-n", "kube-system", "-o", "jsonpath={.status.containerStatuses[0].ready}"])
        if proc.stdout.strip() == "true":
            return f"etcd-{node} ready"
        time.sleep(5)
    return f"timeout waiting for etcd-{node}"


def _restore_manifest(node: str) -> None:
    run_on_node(node, """
set -e
MAN=/etc/kubernetes/manifests/etcd.yaml
if [ -f /tmp/etcd.yaml.clean ]; then
  sudo cp /tmp/etcd.yaml.clean "$MAN"
elif [ -f /tmp/etcd.yaml.orig ]; then
  sudo cp /tmp/etcd.yaml.orig "$MAN"
fi
sudo rm -f /etc/kubernetes/manifests/etcd.yaml.disabled /var/lib/etcd/filler
sudo systemctl restart kubelet
""")


def restart_member(node: str) -> str:
    _restore_manifest(node)
    _kubectl(["kubectl", "delete", "pod", f"etcd-{node}", "-n", "kube-system", "--ignore-not-found=true"], timeout=60)
    return _wait_for_etcd(node)


def member_remove_readd(node: str) -> str:
    ip = node_ip(node)
    resolved = cluster_config.node_by_role(node)
    healthy = next(n for n in cluster_config.nodes() if n != resolved)

    # find member id
    proc = _kubectl(["kubectl", "exec", "-n", "kube-system", f"etcd-{healthy}", "--", "etcdctl",
                     "--endpoints=https://127.0.0.1:2379",
                     "--cacert=/etc/kubernetes/pki/etcd/ca.crt",
                     "--cert=/etc/kubernetes/pki/etcd/server.crt",
                     "--key=/etc/kubernetes/pki/etcd/server.key",
                     "member", "list"])
    member_id = ""
    for line in proc.stdout.splitlines():
        if f", {node}," in line or f" {node}," in line:
            member_id = line.split(",")[0].strip()
            break
    if member_id:
        _kubectl(["kubectl", "exec", "-n", "kube-system", f"etcd-{healthy}", "--", "etcdctl",
                  "--endpoints=https://127.0.0.1:2379",
                  "--cacert=/etc/kubernetes/pki/etcd/ca.crt",
                  "--cert=/etc/kubernetes/pki/etcd/server.crt",
                  "--key=/etc/kubernetes/pki/etcd/server.key",
                  "member", "remove", member_id])

    # add member back
    proc = _kubectl(["kubectl", "exec", "-n", "kube-system", f"etcd-{healthy}", "--", "etcdctl",
                     "--endpoints=https://127.0.0.1:2379",
                     "--cacert=/etc/kubernetes/pki/etcd/ca.crt",
                     "--cert=/etc/kubernetes/pki/etcd/server.crt",
                     "--key=/etc/kubernetes/pki/etcd/server.key",
                     "member", "add", node, f"--peer-urls=https://{ip}:2380"])
    initial_cluster = ""
    for line in proc.stdout.splitlines():
        if "ETCD_INITIAL_CLUSTER=" in line:
            initial_cluster = line.split("=", 1)[1].strip().strip('"')
    if not initial_cluster:
        initial_cluster = f"{node}=https://{ip}:2380"

    # wipe data
    run_on_node(node, "sudo rm -rf /var/lib/etcd/*")

    # restore manifest
    _restore_manifest(node)

    # update manifest fields
    script = f"""
set -e
python3 - <<'PY'
import re
p='/etc/kubernetes/manifests/etcd.yaml'
s=open(p).read()
s=re.sub(r'--initial-advertise-peer-urls=https://[^ ]+', '--initial-advertise-peer-urls=https://{ip}:2380', s)
s=re.sub(r'--initial-cluster=.*', '--initial-cluster={initial_cluster}', s)
if '--initial-cluster-state' not in s:
    s=s.replace('--initial-cluster=' + {initial_cluster!r}, '--initial-cluster=' + {initial_cluster!r} + '\\n    - --initial-cluster-state=existing')
else:
    s=re.sub(r'--initial-cluster-state=\\S+', '--initial-cluster-state=existing', s)
open(p,'w').write(s)
PY
"""
    run_on_node(node, script)
    _kubectl(["kubectl", "delete", "pod", f"etcd-{node}", "-n", "kube-system", "--ignore-not-found=true"], timeout=60)
    return _wait_for_etcd(node)


def kube_etcd_switch_rebuild(node: str) -> str:
    return member_remove_readd(node)


def snapshot_restore_full() -> str:
    reset_script = Path(__file__).resolve().parent.parent / "cluster" / "reset_cluster.sh"
    subprocess.run([str(reset_script)], check=True, timeout=1800)
    return "cluster reset"


def reset_cluster() -> str:
    return snapshot_restore_full()
