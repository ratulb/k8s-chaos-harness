#!/usr/bin/env bash
# Verify a healthy 3-member etcd cluster.
set -euo pipefail

if [[ "$(kubectl get nodes --no-headers 2>/dev/null | wc -l)" -lt 3 ]]; then
  echo "Expected 3 nodes" >&2
  exit 1
fi

ready_nodes=$(kubectl get nodes --no-headers 2>/dev/null | awk '$2=="Ready"{c++} END{print c+0}')
if [[ "$ready_nodes" -lt 3 ]]; then
  echo "Expected 3 Ready nodes, got $ready_nodes" >&2
  exit 1
fi

etcd_pods=$(kubectl get pods -n kube-system -l component=etcd --no-headers 2>/dev/null | awk '$2=="1/1" && $3=="Running"{c++} END{print c+0}')
if [[ "$etcd_pods" -lt 3 ]]; then
  echo "Expected 3 running etcd pods, got $etcd_pods" >&2
  exit 1
fi

for node in vm1 vm2 vm3; do
  pod="etcd-${node}"
  members=$(kubectl exec -n kube-system "$pod" -- etcdctl \
    --endpoints=https://127.0.0.1:2379 \
    --cacert=/etc/kubernetes/pki/etcd/ca.crt \
    --cert=/etc/kubernetes/pki/etcd/server.crt \
    --key=/etc/kubernetes/pki/etcd/server.key \
    member list 2>/dev/null | wc -l)
  if [[ "$members" -ne 3 ]]; then
    echo "$pod: expected 3 members, got $members" >&2
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

echo "etcd healthy: 3/3 members"
