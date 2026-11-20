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
sudo mv "$MAN" "${MAN}.disabled"
REMOTE

# wait until the member is actually down
for i in $(seq 1 30); do
  ready=$(kubectl get pod etcd-${NODE} -n kube-system -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null || true)
  if [ "$ready" != "true" ]; then break; fi
  sleep 2
done
echo "injected etcd-member-down on $NODE"
