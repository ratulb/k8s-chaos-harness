# Kaggle bootstrap

This is the budget-friendly GPU route for people without a paid OpenAI or
Anthropic subscription and without a local GPU. It keeps the harness on the
self-hosted `ollama` backend; Kaggle is only where the GPU runs. If you already
have a GPU, skip this folder and run Ollama locally with
`OLLAMA_BASE_URL=http://127.0.0.1:11434`.

Run `kaggle/bootstrap.sh` in a Kaggle notebook cell with `%%bash`.

It installs sshd, Ollama, pulls `qwen3.8:27b`, and opens a zrok tunnel. Wait until
the cell prints a line like:

```text
zrok2 access private <token>
```

The token is ephemeral. Do not paste it into commits or issues.

## Open the tunnel locally

Keep the Kaggle kernel/cell supervisor alive; if it exits, the tunnel closes.

**Terminal 1** — start the zrok private frontend. It defaults to
`127.0.0.1:9191`:

```bash
zrok2 access private <token>
```

**Terminal 2** — forward the notebook's Ollama port through the SSH tunnel:

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

`OLLAMA_MODEL` must match the model tag pulled by the Kaggle cell. Then verify
the LLM brain first; no cluster is needed for these checks:

```bash
curl http://127.0.0.1:11435/api/tags
python3 scripts/llm_ready.py
./chaos ping "Say hello in one sentence"
python3 -m unittest tests.test_backends -v
```

A successful ping prints `ollama / <model>` and the reply. Once those pass, run a
benchmark with the self-hosted `ollama` backend:

```bash
./chaos run --scenario etcd-member-down --backend ollama --runs 1
```

Notes:

- The final hint printed by `kaggle/bootstrap.sh` omits the `-L
  11435:127.0.0.1:11434` port forward; use the full command above.
- The zrok supervisor is tied to `KERNEL_PID`. If the notebook kernel cell exits,
  the tunnel closes and the token must be re-read/re-created.
- `PUBLIC_KEY_URL` must serve the public key matching the local private key used
  with `-i`; otherwise SSH asks for a password.
- Kaggle is not needed for SQLite/checkpointer development or offline tests.
- Kaggle is infrastructure, not a provider baseline: runs are recorded as the
  `ollama` backend. Keep this folder as the repeatable no-subscription GPU path.
- Never commit tunnel tokens or `.env`; this setup stays local to your machine.
