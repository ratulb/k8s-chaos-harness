#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODES=("$@")
if [[ ${#NODES[@]} -eq 0 ]]; then
  NODES=(vm2 vm3)
fi
for n in "${NODES[@]}"; do
  "$DIR/etcd_member_down.sh" "$n"
done
echo "injected etcd-quorum-loss"
