#!/usr/bin/env bash
# Destroy and rebuild the cluster, then verify etcd health.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config.sh"

K8S_EASY_INSTALL="${K8S_EASY_INSTALL:-$HOME/k8s-easy-install}"

clean_fault_artifacts() {
  local node="$1"
  run_on_node "$node" <<'REMOTE'
sudo rm -f /etc/kubernetes/manifests/etcd.yaml.disabled /tmp/etcd.yaml.orig /var/lib/etcd/filler
REMOTE
}

# Remove stale fault artifacts before rebuild.
for node in "${NODES[@]}"; do
  clean_fault_artifacts "$node" >/dev/null 2>&1 || true
done

cd "$K8S_EASY_INSTALL"
bash cleanup-all.sh
printf 'y\n' | bash launch-cluster.sh

# Remove artifacts again after rebuild.
cd "$ROOT"
for node in "${NODES[@]}"; do
  clean_fault_artifacts "$node" >/dev/null 2>&1 || true
done

for i in $(seq 1 36); do
  if bash "$SCRIPT_DIR/verify_etcd_healthy.sh" >/dev/null 2>&1; then
    bash "$SCRIPT_DIR/snapshot_clean_state.sh"
    echo "cluster reset and verified"
    exit 0
  fi
  sleep 5
done

bash "$SCRIPT_DIR/verify_etcd_healthy.sh"
exit 1
