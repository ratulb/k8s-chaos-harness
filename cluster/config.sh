#!/usr/bin/env bash
# Shared, portable cluster configuration for bash scripts.
#
# Machine names, addresses, and SSH users are resolved from the environment or
# discovered from Kubernetes; see tools/cluster_config.py.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cluster_config() {
  PYTHONPATH="$ROOT" python3 "$ROOT/tools/cluster_config.py" "$@"
}

NODES=()
SSH_USER=""
SSH_KEY=""
PROBE_NODE=""
for _ in $(seq 1 12); do
  if config_env="$(cluster_config env 2>/dev/null)" && [[ -n "$config_env" ]]; then
    eval "$config_env"
    break
  fi
  sleep 5
done
if [[ "${#NODES[@]}" -lt 3 ]]; then
  echo "cannot resolve three cluster nodes; set K8S_NODES in .env" >&2
  exit 1
fi

# Execute a bash script on a node, locally when the node is this host.
run_on_node() {
  local node="$1"
  local target
  target="$(cluster_config ssh-target "$node")"
  if [[ -z "$target" ]]; then
    bash
  else
    local -a ssh_args=(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10)
    [[ -n "$SSH_KEY" ]] && ssh_args+=(-i "$SSH_KEY")
    "${ssh_args[@]}" "$target" bash
  fi
}

# Name of the static etcd pod used to probe cluster-wide etcd health.
etcd_probe_pod() {
  echo "etcd-${PROBE_NODE}"
}

delete_etcd_pod() {
  local node="$1"
  timeout 30s kubectl delete pod "etcd-${node}" -n kube-system \
    --ignore-not-found=true --grace-period=0 --force --request-timeout=10s \
    >/dev/null 2>&1 || true
}
