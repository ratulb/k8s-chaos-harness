#!/usr/bin/env bash
# Stop enough etcd members to remove cluster quorum.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../cluster/config.sh"

TARGETS=("$@")
if [[ ${#TARGETS[@]} -eq 0 ]]; then
  TARGETS=("${QUORUM_LOSS_NODES[@]}")
fi
if [[ ${#TARGETS[@]} -eq 0 ]]; then
  echo "cannot resolve quorum-loss targets; set K8S_NODES or pass node names" >&2
  exit 2
fi
for node in "${TARGETS[@]}"; do
  "$SCRIPT_DIR/etcd_member_down.sh" "$node"
done
survivors=$((ETCD_MEMBER_COUNT - ${#TARGETS[@]}))
if [[ "$survivors" -lt 0 ]]; then
  survivors=0
fi
for i in $(seq 1 60); do
  ready=$(kubectl --request-timeout=10s get nodes --no-headers 2>/dev/null | awk '$2=="Ready"{c++} END{print c+0}' || echo 0)
  if [[ "$ready" -le "$survivors" ]]; then
    break
  fi
  sleep 1
done
echo "injected etcd-quorum-loss on ${TARGETS[*]} (quorum $QUORUM of $ETCD_MEMBER_COUNT)"
