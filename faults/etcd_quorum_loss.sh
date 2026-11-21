#!/usr/bin/env bash
# Stop two etcd members to remove cluster quorum.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../cluster/config.sh"

NODES=("$@")
if [[ ${#NODES[@]} -eq 0 ]]; then
  NODES=("${SECOND_NODE}" "${THIRD_NODE}")
fi
for node in "${NODES[@]}"; do
  "$SCRIPT_DIR/etcd_member_down.sh" "$node"
done
for i in $(seq 1 60); do
  ready=$(kubectl --request-timeout=10s get nodes --no-headers 2>/dev/null | awk '$2=="Ready"{c++} END{print c+0}')
  if [[ "$ready" -le 1 ]]; then
    break
  fi
  sleep 1
done
echo "injected etcd-quorum-loss on ${NODES[*]}"
