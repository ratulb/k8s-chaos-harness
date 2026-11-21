#!/usr/bin/env bash
# Verify a healthy N-member etcd cluster without assuming node names.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config.sh"

EXPECTED="${EXPECTED_ETCD_MEMBERS:-${#NODES[@]}}"

nodes_out=""
for i in $(seq 1 12); do
  nodes_out=$(kubectl --request-timeout=10s get nodes --no-headers 2>/dev/null || true)
  [[ "$(echo "$nodes_out" | wc -l)" -ge "$EXPECTED" ]] && break
  sleep 5
done
if [[ "$(echo "$nodes_out" | wc -l)" -lt "$EXPECTED" ]]; then
  echo "Expected at least $EXPECTED nodes" >&2
  exit 1
fi

ready_nodes=0
for i in $(seq 1 24); do
  ready_nodes=$(echo "$nodes_out" | awk '$2=="Ready"{c++} END{print c+0}')
  [[ "$ready_nodes" -ge "$EXPECTED" ]] && break
  sleep 5
  nodes_out=$(kubectl --request-timeout=10s get nodes --no-headers 2>/dev/null || echo "$nodes_out")
done
if [[ "$ready_nodes" -lt "$EXPECTED" ]]; then
  echo "Expected $EXPECTED Ready nodes, got $ready_nodes" >&2
  exit 1
fi

etcd_pods=$(kubectl --request-timeout=10s get pods -n kube-system -l component=etcd \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null || true)
running=$(for pod in $etcd_pods; do
  ready=$(kubectl --request-timeout=10s get pod "$pod" -n kube-system \
    -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null || true)
  [[ "$ready" == "true" ]] && echo "$pod"
done | wc -l)
if [[ "$running" -lt "$EXPECTED" ]]; then
  echo "Expected $EXPECTED running etcd pods, got $running" >&2
  exit 1
fi

for pod in $etcd_pods; do
  members=$(kubectl exec -n kube-system "$pod" -- etcdctl \
    --endpoints=https://127.0.0.1:2379 \
    --cacert=/etc/kubernetes/pki/etcd/ca.crt \
    --cert=/etc/kubernetes/pki/etcd/server.crt \
    --key=/etc/kubernetes/pki/etcd/server.key \
    member list 2>/dev/null | wc -l || true)
  if [[ "$members" -ne "$EXPECTED" ]]; then
    echo "$pod: expected $EXPECTED members, got $members" >&2
    exit 1
  fi
  if ! kubectl exec -n kube-system "$pod" -- etcdctl \
      --endpoints=https://127.0.0.1:2379 \
      --cacert=/etc/kubernetes/pki/etcd/ca.crt \
      --cert=/etc/kubernetes/pki/etcd/server.crt \
      --key=/etc/kubernetes/pki/etcd/server.key \
      endpoint health >/dev/null 2>&1; then
    echo "$pod: etcd endpoint unhealthy" >&2
    exit 1
  fi
done

echo "etcd healthy: $EXPECTED/$EXPECTED members"
