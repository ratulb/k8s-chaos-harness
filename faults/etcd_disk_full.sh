#!/usr/bin/env bash
set -euo pipefail
NODE=${1:?node required}
IP=$(kubectl get node "$NODE" -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')

run_node() {
  if [[ "$NODE" == "$(hostname)" ]]; then bash; else ssh -o StrictHostKeyChecking=no "$IP" bash; fi
}

run_node <<'REMOTE'
set -e
avail=$(df -k /var/lib/etcd | awk 'NR==2{print $4}')
sudo fallocate -l $(( (avail - 1024) * 1024 )) /var/lib/etcd/filler 2>/dev/null || \
  sudo dd if=/dev/zero of=/var/lib/etcd/filler bs=1M count=$(( (avail / 1024) - 1 )) status=none
REMOTE

echo "injected etcd-disk-full on $NODE"
