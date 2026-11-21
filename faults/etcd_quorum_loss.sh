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
echo "injected etcd-quorum-loss on ${NODES[*]}"
