#!/usr/bin/env bash
# Snapshot the clean etcd static-pod manifest on every control-plane node.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config.sh"

PYTHONPATH="$ROOT" python3 - <<'PY'
from tools import cluster_config
from tools.ssh import run_on_node

script = """
set -e
sudo cp /etc/kubernetes/manifests/etcd.yaml /tmp/etcd.yaml.clean
sudo chmod 644 /tmp/etcd.yaml.clean
"""
names = cluster_config.nodes()
for node in names:
    rc, out, err = run_on_node(node, script)
    if rc != 0:
        raise SystemExit(f"snapshot failed on {node}: {out} {err}")
print(f"etcd manifests snapshotted to /tmp/etcd.yaml.clean on {' '.join(names)}")
PY

cluster_config cache-ips
