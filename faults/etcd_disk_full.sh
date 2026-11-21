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
sudo fallocate -l $(( (avail - 1024) * 1024 )) /var/lib/etcd/filler 2>/dev/null || \
  sudo dd if=/dev/zero of=/var/lib/etcd/filler bs=1M count=$(( (avail / 1024) - 1 )) status=none
REMOTE

for i in $(seq 1 60); do
  if ! kubectl exec -n kube-system "$(etcd_probe_pod)" -- etcdctl \
      --endpoints=https://127.0.0.1:2379 \
      --cacert=/etc/kubernetes/pki/etcd/ca.crt \
      --cert=/etc/kubernetes/pki/etcd/server.crt \
      --key=/etc/kubernetes/pki/etcd/server.key \
      endpoint health --cluster >/dev/null 2>&1; then
    echo "injected etcd-disk-full on $NODE"
    exit 0
  fi
  sleep 2
done

echo "failed to inject etcd-disk-full on $NODE: cluster still healthy" >&2
exit 1
