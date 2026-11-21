#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="${BACKEND:-ollama}"
ids=(control-healthy-noisy etcd-disk-full etcd-member-down etcd-member-wiped etcd-quorum-loss etcd-wrong-peer-url)
labels=('Healthy control' 'Etcd disk full' 'Etcd member down' 'Etcd member wiped' 'Etcd quorum loss' 'Etcd wrong peer URL')

for i in "${!labels[@]}"; do
  printf "%2d) %s\n" "$((i + 1))" "${labels[$i]}"
done
printf "Scenario [1-%d]: " "${#labels[@]}"
read -r choice
if ! [[ "$choice" =~ ^[0-9]+$ ]] || (( choice < 1 || choice > ${#labels[@]} )); then
  echo "invalid scenario selection" >&2
  exit 2
fi
id="${ids[$((choice - 1))]}"
exec bash "$ROOT/run_benchmark.sh" --scenario "$id" --backend "$BACKEND" "$@"
