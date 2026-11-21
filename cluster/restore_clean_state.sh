#!/usr/bin/env bash
# Restore clean etcd manifests and remove fault artifacts on every node.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config.sh"

PYTHONPATH="$ROOT" python3 - <<'PY'
from tools import cluster_config
from tools.ssh import run_on_node

cleanup = """
set -e
sudo pkill -f 'dd.*etcd/filler' 2>/dev/null || true
sudo rm -f /etc/kubernetes/manifests/etcd.yaml.disabled /var/lib/etcd/filler
if [ -f /tmp/etcd.yaml.clean ]; then
  sudo cp /tmp/etcd.yaml.clean /etc/kubernetes/manifests/etcd.yaml
elif [ -f /tmp/etcd.yaml.orig ]; then
  sudo cp /tmp/etcd.yaml.orig /etc/kubernetes/manifests/etcd.yaml
fi
sudo systemctl restart kubelet 2>/dev/null || true
"""
for node in cluster_config.nodes():
    rc, out, err = run_on_node(node, cleanup)
    if rc != 0:
        raise SystemExit(f"cleanup failed on {node}: {out} {err}")
PY

kubectl delete event scary-warning-noisy -n default --ignore-not-found=true --request-timeout=10s >/dev/null 2>&1 || true

verify_or_repair() {
  local tries="$1"
  for _ in $(seq 1 "$tries"); do
    if bash "$SCRIPT_DIR/verify_etcd_healthy.sh" >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  return 1
}

if verify_or_repair 12; then
  cluster_config cache-ips >/dev/null 2>&1 || true
  echo "clean etcd state restored"
  exit 0
fi

# Only if a plain manifest restore was insufficient, rebuild members whose
# containers are not ready (for example after a wiped data directory).
PYTHONPATH="$ROOT" python3 - <<'PY'
import subprocess

from tools import cluster_config
from tools.repairs import member_remove_readd


def etcd_ready(node: str) -> bool:
    proc = subprocess.run(
        ["kubectl", "get", "pod", f"etcd-{node}", "-n", "kube-system",
         "-o", "jsonpath={.status.containerStatuses[0].ready}", "--request-timeout=10s"],
        capture_output=True, text=True, timeout=20, check=False,
    )
    return proc.stdout.strip() == "true"


for node in cluster_config.nodes():
    if not etcd_ready(node):
        print(f"repairing etcd member state on {node}", flush=True)
        print(member_remove_readd(node), flush=True)
PY

if verify_or_repair 12; then
  cluster_config cache-ips >/dev/null 2>&1 || true
  echo "clean etcd state restored"
  exit 0
fi

echo "failed to restore clean etcd state" >&2
bash "$SCRIPT_DIR/verify_etcd_healthy.sh" >&2 || true
exit 1
