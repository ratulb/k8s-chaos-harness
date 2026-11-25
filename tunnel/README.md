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

## Add a second model endpoint

The harness already accepts arbitrary OpenAI-compatible endpoints through
`CHAOS_LLM_ENDPOINTS`; no new backend code is needed. To serve Bonsai 2.5 27B on
a separate Kaggle GPU, start the remote runtime with the Ollama tag from the
model card:

```bash
MODEL='hf.co/prism-ml/Ternary-Bonsai-2.5-27B-GGUF:Q2_0' bash tunnel/bootstrap.sh
```

Read the new ephemeral token from the bootstrap output. Open a second frontend
and SSH forward to a different local port:

```bash
zrok2 access private --bind 127.0.0.1:9192 <bonsai-token>
ssh -N \
  -o UserKnownHostsFile=/dev/null \
  -o StrictHostKeyChecking=no \
  -i ~/.ssh/id_rsa \
  -p 9192 \
  root@127.0.0.1 \
  -L 11436:127.0.0.1:11434
```

Add the endpoint to the repo's `.env` while keeping the existing `OLLAMA_*`
settings for the first model:

```dotenv
CHAOS_LLM_ENDPOINTS=[{"name":"bonsai","base_url":"http://127.0.0.1:11436","model":"hf.co/prism-ml/Ternary-Bonsai-2.5-27B-GGUF:Q2_0","priority":5}]
```

Lower `priority` values come first under `--backend auto`. Raise the number if
the existing `ollama` endpoint should remain the primary. The same JSON can use
`http://127.0.0.1:11435` when both models are pulled into one remote Ollama
instance served through one tunnel.

Verify the new endpoint without touching the cluster:

```bash
curl http://127.0.0.1:11436/api/tags
python3 scripts/llm_ready.py
./chaos ping "Reply with OK" --backend bonsai
python3 -m unittest tests.test_backends.TestBackendDiscovery -v
CHAOS_LIVE_BACKEND_TESTS=1 python3 -m unittest tests.test_backends -v
```

The live smoke test now includes custom endpoints from `CHAOS_LLM_ENDPOINTS`.
A benchmark selects it explicitly with `--backend bonsai`. The harness sends
`temperature=0.2` and `max_tokens=2048` to every backend for comparability;
Bonsai 2.5 reasons by default, so watch for runs that end mid-thought or emit no
tool call before treating scores as comparable.

Notes:

- The final hint printed by `tunnel/bootstrap.sh` omits the `-L
  11435:127.0.0.1:11434` port forward; use the full command above.
- The zrok supervisor is tied to `SESSION_PID`. If that process exits, the
  tunnel closes and the token must be re-read/re-created.
- `PUBLIC_KEY_URL` must serve the public key matching the local private key used
  with `-i`; otherwise SSH asks for a password.
- A local Ollama instance works too; this tunnel is only needed for remote runtimes.
