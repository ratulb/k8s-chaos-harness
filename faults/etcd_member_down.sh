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
if [ -f /tmp/etcd.yaml.clean ]; then
  sudo cp /tmp/etcd.yaml.clean "$MAN"
elif [ ! -f /tmp/etcd.yaml.orig ]; then
  sudo cp "$MAN" /tmp/etcd.yaml.orig
fi
sudo python3 - <<'PY'
import re
p = '/etc/kubernetes/manifests/etcd.yaml'
s = open(p).read()
s, n = re.subn(r'(  - command:\n)(?:    - .*\n)+', '  - command:\n    - etcd\n    - --version\n', s, count=1)
if n != 1:
    raise SystemExit('etcd command block not found')
open(p, 'w').write(s)
PY
sudo systemctl restart kubelet
REMOTE

for i in $(seq 1 60); do
  if ! kubectl exec -n kube-system etcd-vm1 -- etcdctl \
      --endpoints=https://127.0.0.1:2379 \
      --cacert=/etc/kubernetes/pki/etcd/ca.crt \
      --cert=/etc/kubernetes/pki/etcd/server.crt \
      --key=/etc/kubernetes/pki/etcd/server.key \
      endpoint health --cluster >/dev/null 2>&1; then
    echo "injected etcd-member-down on $NODE"
    exit 0
  fi
  sleep 2
done

echo "failed to inject etcd-member-down on $NODE: cluster still healthy" >&2
exit 1
