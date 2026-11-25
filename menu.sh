#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="${BACKEND:-ollama}"
ids=(ping control-healthy-noisy etcd-disk-full etcd-member-down etcd-member-wiped etcd-quorum-loss etcd-wrong-peer-url)
labels=('Ping LLM (auto fallback)' 'Healthy control' 'Etcd disk full' 'Etcd member down' 'Etcd member wiped' 'Etcd quorum loss' 'Etcd wrong peer URL')

for i in "${!labels[@]}"; do
  printf "%2d) %s\n" "$((i + 1))" "${labels[$i]}"
done
printf "Action [1-%d]: " "${#labels[@]}"
read -r choice
if ! [[ "$choice" =~ ^[0-9]+$ ]] || (( choice < 1 || choice > ${#labels[@]} )); then
  echo "invalid selection" >&2
  exit 2
fi
id="${ids[$((choice - 1))]}"
cd "$ROOT"
if [[ "$id" == "ping" ]]; then
  exec ./chaos ping "$@"
fi
exec ./chaos run --scenario "$id" --backend "$BACKEND" "$@"
