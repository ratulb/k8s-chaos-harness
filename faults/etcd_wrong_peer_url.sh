#!/usr/bin/env bash
# Break one member's peer URL so it cannot rejoin raft traffic.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../cluster/config.sh"

NODE=${1:?node required}
TARGET_IP="$(cluster_config ip "$NODE")"
PROBE="$(cluster_config probe)"
if [[ "$PROBE" == "$NODE" ]]; then
  for candidate in "${CONTROL_PLANES[@]}"; do
    if [[ "$candidate" != "$NODE" ]]; then
      PROBE="$candidate"
      break
    fi
  done
fi
if [[ "$PROBE" == "$NODE" ]]; then
  echo "cannot find a probe node other than $NODE" >&2
  exit 2
fi

MEMBERS="$(kubectl --request-timeout=10s exec -n kube-system "etcd-$PROBE" -- \
  etcdctl --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  --write-out=json member list)"
MEMBER_ID="$(python3 -c '
import json
import sys

node = sys.argv[1]
address = sys.argv[2]
payload = json.load(sys.stdin)
for member in payload.get("members", []):
    peer = member.get("peerURLs") or member.get("peerUrls") or ""
    if isinstance(peer, list):
        peer = " ".join(str(item) for item in peer)
    client = member.get("clientURLs") or ""
    if isinstance(client, list):
        client = " ".join(str(item) for item in client)
    if node == str(member.get("name", "")) or address in str(peer) or address in str(client):
        print(member.get("id") or member.get("ID"))
        break
else:
    raise SystemExit(f"cannot find etcd member for {node} ({address})")
' "$NODE" "$TARGET_IP" <<<"$MEMBERS")"

kubectl --request-timeout=10s exec -n kube-system "etcd-$PROBE" -- \
  etcdctl --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  member update "$MEMBER_ID" --peer-urls="https://$TARGET_IP:2399"

echo "injected etcd-wrong-peer-url on $NODE (registered peer https://$TARGET_IP:2399)"
