#!/usr/bin/env bash
# Stop serving etcd on one member by replacing its static-pod command.
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

# Wait until the cluster-wide etcd health check actually fails; pod readiness
# alone can be stale while kubelet/apiserver recover.
for i in $(seq 1 60); do
  if ! kubectl exec -n kube-system "$(etcd_probe_pod)" -- etcdctl \
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
