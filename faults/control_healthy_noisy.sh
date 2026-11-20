#!/usr/bin/env bash
set -euo pipefail
kubectl delete event scary-warning-noisy -n default --ignore-not-found=true >/dev/null 2>&1 || true
kubectl apply -f - >/dev/null 2>&1 <<'EOF'
apiVersion: v1
kind: Event
metadata:
  name: scary-warning-noisy
  namespace: default
type: Warning
reason: BackOff
message: "Back-off restarting failed container (scary but harmless)"
source:
  component: kubelet
involvedObject:
  kind: Pod
  name: noisy-warning-pod
  namespace: default
  apiVersion: v1
EOF
echo "injected control-healthy-noisy"
