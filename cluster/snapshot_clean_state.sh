#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONPATH="$ROOT" python3 - <<'PY'
from tools.ssh import run_on_node

script = """
set -e
sudo cp /etc/kubernetes/manifests/etcd.yaml /tmp/etcd.yaml.clean
sudo chmod 644 /tmp/etcd.yaml.clean
"""
for node in ("vm1", "vm2", "vm3"):
    rc, out, err = run_on_node(node, script)
    if rc != 0:
        raise SystemExit(f"snapshot failed on {node}: {out} {err}")
print("etcd manifests snapshotted to /tmp/etcd.yaml.clean on vm1 vm2 vm3")
PY
