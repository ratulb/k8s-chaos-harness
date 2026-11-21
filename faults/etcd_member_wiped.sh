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
sleep 5
echo "injected etcd-member-wiped on $NODE"
