#!/usr/bin/env bash
# Install/launch Ollama and pull a model.
#
# Usage:
#   scripts/install_ollama.sh                 # default: Kaggle-sized qwen3.8:27b
#   scripts/install_ollama.sh small            # local-friendly qwen3:8b
#   scripts/install_ollama.sh qwen3:8b         # explicit model
#   MODEL=qwen3:8b scripts/install_ollama.sh   # env override

set -euo pipefail

if [ "${1:-}" = "small" ]; then
  MODEL_NAME="qwen3:8b"
elif [ -n "${1:-}" ]; then
  MODEL_NAME="$1"
else
  MODEL_NAME="${MODEL:-qwen3.8:27b}"
fi

echo "Target model: $MODEL_NAME"

if ! command -v ollama >/dev/null 2>&1; then
  echo "Installing Ollama..."
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq >/dev/null 2>&1 || true
    apt-get install -y -qq zstd >/dev/null 2>&1 || true
  fi
  curl -fsSL https://ollama.com/install.sh | sh
fi

if ! pgrep -x ollama >/dev/null 2>&1; then
  echo "Starting Ollama server..."
  nohup ollama serve >/tmp/ollama.log 2>&1 &
  sleep 5
fi

pgrep -x ollama >/dev/null 2>&1 && echo "Ollama running" || { echo "Ollama failed to start"; cat /tmp/ollama.log 2>/dev/null || true; exit 1; }

echo "Pulling $MODEL_NAME..."
ollama pull "$MODEL_NAME"
ollama list

if command -v curl >/dev/null 2>&1; then
  if curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    echo "Health check: OK"
  else
    echo "Health check: FAILED"
    exit 1
  fi
fi
