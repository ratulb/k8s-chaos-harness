#!/usr/bin/env bash
# Break one member's peer URL so it cannot rejoin raft traffic.
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
sudo sed -i 's#--initial-advertise-peer-urls=https://\([^:]*\):2380#--initial-advertise-peer-urls=https://\1:2399#' "$MAN"
sudo sed -i 's#--listen-peer-urls=https://\([^:]*\):2380#--listen-peer-urls=https://\1:2399#' "$MAN"
sudo systemctl restart kubelet
REMOTE

delete_etcd_pod "$NODE"
sleep 5
echo "injected etcd-wrong-peer-url on $NODE"
