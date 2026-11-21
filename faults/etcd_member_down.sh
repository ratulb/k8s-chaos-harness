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

delete_etcd_pod "$NODE"
ready="true"
for _ in $(seq 1 60); do
  ready=$(kubectl --request-timeout=5s get pod "etcd-$NODE" -n kube-system \
    -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null || true)
  if [[ "$ready" != "true" ]]; then
    break
  fi
  sleep 1
done
if [[ "$ready" == "true" ]]; then
  echo "etcd member $NODE did not go down" >&2
  exit 1
fi
echo "injected etcd-member-down on $NODE"
