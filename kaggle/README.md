# Kaggle bootstrap

Run `kaggle/bootstrap.sh` in a Kaggle notebook cell with `%%bash`.

It installs sshd, Ollama, pulls `qwen3.8:27b`, and opens a zrok tunnel.

After it prints the token, run locally:

```bash
zrok2 access private <token>
ssh -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -i ~/.ssh/id_rsa -p 9191 root@127.0.0.1
```

Then set `OLLAMA_BASE_URL=http://127.0.0.1:11434` locally.
