#!/usr/bin/env bash
# Run this in a Kaggle notebook cell with: %%bash
# It installs sshd, Ollama, pulls the model, and opens a zrok tunnel.

set -euo pipefail

# Kaggle exposes the NVIDIA driver under /opt/bin and /usr/local/nvidia.
export PATH=/opt/bin:${PATH}
export LD_LIBRARY_PATH=/usr/local/nvidia/lib64:${LD_LIBRARY_PATH:-}

export PUBLIC_KEY_URL="${PUBLIC_KEY_URL:-https://raw.githubusercontent.com/ratulb/tenmo/refs/heads/main/pub_id}"
export ZROK_TOKEN="${ZROK_TOKEN:-bGp9siRWVlq0}"
export MODEL="${MODEL:-qwen3.8:27b}"
export KERNEL_PID="${KERNEL_PID:-$PPID}"

echo "=== SSH ==="
if ! command -v sshd >/dev/null; then
  apt-get update -qq >/dev/null 2>&1
  apt-get install -y -qq openssh-server >/dev/null 2>&1
fi
mkdir -p /var/run/sshd /root/.ssh
wget -qO /root/.ssh/authorized_keys "$PUBLIC_KEY_URL" || echo "Warning: could not fetch public key"
chmod 700 /root/.ssh && chmod 600 /root/.ssh/authorized_keys
ssh-keygen -A >/dev/null 2>&1
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
sed -i 's/^#\?PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config
pgrep -x sshd >/dev/null || /usr/sbin/sshd
sleep 1
pgrep -x sshd >/dev/null && echo "sshd running" || { echo "sshd failed"; exit 1; }

echo "=== Ollama ==="
if ! command -v ollama >/dev/null; then
  apt-get update -qq >/dev/null 2>&1
  apt-get install -y -qq zstd >/dev/null 2>&1
  curl -fsSL https://ollama.com/install.sh | sh
fi
if ! pgrep -x ollama >/dev/null; then
  nohup ollama serve >/tmp/ollama.log 2>&1 &
  sleep 5
fi
ollama pull "$MODEL"
ollama list

echo "=== zrok2 ==="
if ! command -v zrok2 >/dev/null; then
  URL=$(curl -s https://api.github.com/repos/openziti/zrok/releases/latest \
        | grep -oP '"browser_download_url": "\K[^"]*linux_amd64\.tar\.gz' | head -1)
  curl -sSfL "$URL" | tar -xz -C /usr/local/bin
fi

PIDFILE=/tmp/zrok_supervisor.pid
TOKENFILE=/tmp/zrok_share_token

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null && [ -s "$TOKENFILE" ]; then
  echo "Tunnel already running."
  echo "  zrok2 access private $(cat "$TOKENFILE")"
  exit 0
fi

cat > /tmp/zrok_supervisor.sh <<'EOF'
#!/bin/bash
KPID=$1
echo $$ > /tmp/zrok_supervisor.pid
cleanup() {
  [ -n "${ZPID:-}" ] && kill "$ZPID" 2>/dev/null
  sleep 1
  zrok2 disable </dev/null >>/tmp/zrok_supervisor.log 2>&1
  pkill -x sshd 2>/dev/null
  rm -f /tmp/zrok_supervisor.pid /tmp/zrok_share_token
}
trap cleanup EXIT
trap 'exit 0' TERM INT HUP

zrok2 disable </dev/null >/dev/null 2>&1 || true
zrok2 enable "$ZROK_TOKEN" </dev/null >>/tmp/zrok_supervisor.log 2>&1 || exit 1

rm -f /tmp/zrok_out.log /tmp/zrok_share_token
zrok2 share private --backend-mode tcpTunnel localhost:22 --headless \
  > /tmp/zrok_out.log 2>&1 </dev/null &
ZPID=$!

for i in $(seq 1 60); do
  T=$(grep -oP 'access private \K[a-zA-Z0-9]+' /tmp/zrok_out.log | tail -1)
  [ -n "$T" ] && { echo "$T" > /tmp/zrok_share_token; break; }
  sleep 1
done

while kill -0 "$KPID" 2>/dev/null && kill -0 "$ZPID" 2>/dev/null; do sleep 5; done
EOF
chmod +x /tmp/zrok_supervisor.sh

setsid nohup /tmp/zrok_supervisor.sh "$KERNEL_PID" >/tmp/zrok_supervisor.log 2>&1 </dev/null &

for i in $(seq 1 70); do
  [ -s "$TOKENFILE" ] && break
  sleep 1
done
if [ ! -s "$TOKENFILE" ]; then
  echo "ERROR: no tunnel token. Logs:"
  cat /tmp/zrok_supervisor.log
  cat /tmp/zrok_out.log 2>/dev/null || true
  exit 1
fi

echo "======================================"
echo "  zrok2 access private $(cat "$TOKENFILE")"
echo "======================================"
echo "Then run locally:"
echo "  zrok2 access private $(cat "$TOKENFILE")"
echo "  ssh -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -i ~/.ssh/id_rsa -p 9191 root@127.0.0.1"
