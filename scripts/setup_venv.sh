#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# Install venv support (if not already installed)
sudo apt update
sudo apt install python3-venv python3-pip -y


if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "Virtual environment ready at $ROOT/.venv"
echo "Activate with: source .venv/bin/activate"
