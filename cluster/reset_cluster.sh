#!/usr/bin/env bash
# Destroy and rebuild the cluster, then verify etcd health.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
K8S_EASY_INSTALL="${K8S_EASY_INSTALL:-/home/azureuser/k8s-easy-install}"

# remove stale fault artifacts before rebuild
for ip in 10.1.0.4 10.2.0.4 10.3.0.4; do
  ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$ip" 'sudo rm -f /etc/kubernetes/manifests/etcd.yaml.disabled /tmp/etcd.yaml.orig /var/lib/etcd/filler' >/dev/null 2>&1 || true
done
sudo rm -f /etc/kubernetes/manifests/etcd.yaml.disabled /tmp/etcd.yaml.orig /var/lib/etcd/filler

cd "$K8S_EASY_INSTALL"
bash cleanup-all.sh
printf 'y\n' | bash launch-cluster.sh

# remove stale fault artifacts after rebuild
for ip in 10.1.0.4 10.2.0.4 10.3.0.4; do
  ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$ip" 'sudo rm -f /etc/kubernetes/manifests/etcd.yaml.disabled /tmp/etcd.yaml.orig /var/lib/etcd/filler' >/dev/null 2>&1 || true
done

cd "$ROOT"
exec "$(dirname "${BASH_SOURCE[0]}")/verify_etcd_healthy.sh"
