#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONPATH="$ROOT" python3 - <<'PY'
import subprocess
import time

from tools.ssh import run_on_node
from tools.repairs import member_remove_readd

NODES = ("vm1", "vm2", "vm3")

cleanup = """
set -e
sudo rm -f /etc/kubernetes/manifests/etcd.yaml.disabled /var/lib/etcd/filler
if [ -f /tmp/etcd.yaml.clean ]; then
  sudo cp /tmp/etcd.yaml.clean /etc/kubernetes/manifests/etcd.yaml
elif [ -f /tmp/etcd.yaml.orig ]; then
  sudo cp /tmp/etcd.yaml.orig /etc/kubernetes/manifests/etcd.yaml
fi
sudo systemctl restart kubelet 2>/dev/null || true
"""
for node in NODES:
    rc, out, err = run_on_node(node, cleanup)
    if rc != 0:
        raise SystemExit(f"cleanup failed on {node}: {out} {err}")

subprocess.run(
    ["kubectl", "delete", "event", "scary-warning-noisy", "-n", "default", "--ignore-not-found=true", "--request-timeout=10s"],
    capture_output=True,
    text=True,
    timeout=30,
    check=False,
)


def etcd_ready(node: str) -> bool:
    try:
        proc = subprocess.run(
            ["kubectl", "get", "pod", f"etcd-{node}", "-n", "kube-system",
             "-o", "jsonpath={.status.containerStatuses[0].ready}", "--request-timeout=10s"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        return proc.stdout.strip() == "true"
    except Exception:
        return False


deadline = time.time() + 60
while time.time() < deadline:
    if all(etcd_ready(node) for node in NODES):
        break
    time.sleep(5)

for node in NODES:
    if not etcd_ready(node):
        print(f"repairing etcd member state on {node}", flush=True)
        print(member_remove_readd(node), flush=True)
PY

for i in $(seq 1 36); do
  if bash "$ROOT/cluster/verify_etcd_healthy.sh" >/dev/null 2>&1; then
    echo "clean etcd state restored"
    exit 0
  fi
  sleep 5
done

echo "failed to restore clean etcd state" >&2
bash "$ROOT/cluster/verify_etcd_healthy.sh" >&2 || true
exit 1
