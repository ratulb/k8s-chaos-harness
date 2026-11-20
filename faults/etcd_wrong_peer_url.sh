#!/usr/bin/env bash
set -euo pipefail
NODE=${1:?node required}
IP=$(kubectl get node "$NODE" -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')

run_node() {
  if [[ "$NODE" == "$(hostname)" ]]; then bash; else ssh -o StrictHostKeyChecking=no "$IP" bash; fi
}

run_node <<'REMOTE'
set -e
MAN=/etc/kubernetes/manifests/etcd.yaml
if [ ! -f "$MAN" ] && [ -f "${MAN}.disabled" ]; then sudo mv "${MAN}.disabled" "$MAN"; fi
BACKUP=/tmp/etcd.yaml.orig
if [ ! -f "$BACKUP" ]; then sudo cp "$MAN" "$BACKUP"; fi
sudo sed -i 's#--initial-advertise-peer-urls=https://\([^:]*\):2380#--initial-advertise-peer-urls=https://\1:2399#' "$MAN"
sudo sed -i 's#--listen-peer-urls=https://\([^:]*\):2380#--listen-peer-urls=https://\1:2399#' "$MAN"
REMOTE

sleep 8
echo "injected etcd-wrong-peer-url on $NODE"
