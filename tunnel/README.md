# Remote Ollama tunnel

Run `tunnel/bootstrap.sh` on a remote host with an Ollama-compatible runtime.

It installs sshd, Ollama, pulls the configured model, and opens a zrok tunnel.
Wait until the output prints a line like:

```text
zrok2 access private <token>
```

The token is ephemeral. Do not paste it into commits or issues.

## Open the tunnel locally

Keep the remote supervisor process alive; if it exits, the tunnel closes.

**Terminal 1** — start the zrok private frontend. It defaults to
`127.0.0.1:9191`:

```bash
zrok2 access private <token>
```

**Terminal 2** — forward the remote Ollama port through the SSH tunnel:

```bash
ssh -N \
  -o UserKnownHostsFile=/dev/null \
  -o StrictHostKeyChecking=no \
  -i ~/.ssh/id_rsa \
  -p 9191 \
  root@127.0.0.1 \
  -L 11435:127.0.0.1:11434
```

Use port `11435` locally because the harness host may already have Ollama
listening on `11434`. Keep this SSH process open.

If `9191` or `11435` is already in use, change it consistently:

```bash
zrok2 access private --bind 127.0.0.1:9192 <token>
ssh -N ... -p 9192 root@127.0.0.1 -L 11436:127.0.0.1:11434
```

## Configure and verify the harness

Set these in the repo's `.env`:

```dotenv
OLLAMA_BASE_URL=http://127.0.0.1:11435
OLLAMA_MODEL=qwen3.8:27b
OLLAMA_API_KEY=ollama
```

`OLLAMA_MODEL` must match the model tag pulled by `tunnel/bootstrap.sh`. Then
verify:

```bash
curl http://127.0.0.1:11435/api/tags
python3 scripts/llm_ready.py
python3 -m unittest tests.test_backends -v
```

Once those pass, run a benchmark with the self-hosted `ollama` backend:

```bash
./run_benchmark.sh --scenario etcd-member-down --backend ollama --runs 1
```

Notes:

- The final hint printed by `tunnel/bootstrap.sh` omits the `-L
  11435:127.0.0.1:11434` port forward; use the full command above.
- The zrok supervisor is tied to `SESSION_PID`. If that process exits, the
  tunnel closes and the token must be re-read/re-created.
- `PUBLIC_KEY_URL` must serve the public key matching the local private key used
  with `-i`; otherwise SSH asks for a password.
- A local Ollama instance works too; this tunnel is only needed for remote runtimes.
