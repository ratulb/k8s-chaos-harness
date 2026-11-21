#!/usr/bin/env bash
# Fill the filesystem that backs etcd data on one member.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../cluster/config.sh"

NODE=${1:?node required}

run_on_node "$NODE" <<'REMOTE'
set -e
sudo rm -f /var/lib/etcd/filler
avail=$(df -k /var/lib/etcd | awk 'NR==2{print $4}')
if ! sudo fallocate -l $(( (avail - 1024) * 1024 )) /var/lib/etcd/filler 2>/dev/null; then
  sudo dd if=/dev/zero of=/var/lib/etcd/filler bs=1M count=$(( (avail / 1024) - 1 )) status=none >/dev/null 2>&1 || true
fi
REMOTE

sleep 10
echo "injected etcd-disk-full on $NODE"
