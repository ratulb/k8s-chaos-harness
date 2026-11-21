#!/usr/bin/env bash
# Wipe the data directory of one etcd member.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../cluster/config.sh"

NODE=${1:?node required}

run_on_node "$NODE" <<'REMOTE'
set -e
MAN=/etc/kubernetes/manifests/etcd.yaml
if [ -f /tmp/etcd.yaml.clean ]; then
  sudo cp /tmp/etcd.yaml.clean "$MAN"
elif [ ! -f /tmp/etcd.yaml.orig ]; then
  sudo cp "$MAN" /tmp/etcd.yaml.orig
fi
sudo rm -rf /var/lib/etcd/*
sudo systemctl restart kubelet
REMOTE

kubectl delete pod "etcd-${NODE}" -n kube-system --ignore-not-found=true >/dev/null 2>&1 || true

for i in $(seq 1 60); do
  if ! kubectl exec -n kube-system "$(etcd_probe_pod)" -- etcdctl \
      --endpoints=https://127.0.0.1:2379 \
      --cacert=/etc/kubernetes/pki/etcd/ca.crt \
      --cert=/etc/kubernetes/pki/etcd/server.crt \
      --key=/etc/kubernetes/pki/etcd/server.key \
      endpoint health --cluster >/dev/null 2>&1; then
    echo "injected etcd-member-wiped on $NODE"
    exit 0
  fi
  sleep 2
done

echo "failed to inject etcd-member-wiped on $NODE: cluster still healthy" >&2
exit 1
