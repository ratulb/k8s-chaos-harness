# scripts/

Helper scripts for environment setup and backend checks.

## Prerequisites

- Linux (Ubuntu/Debian for `setup_venv.sh` and `install_ollama.sh`)
- `python3`, `sudo` (for apt-based setup)
- Network access for pip/model downloads
- A populated `.env` (copy from `.env.example` at repo root)

## setup_venv.sh

Creates `.venv` and installs `requirements.txt`.

```bash
bash scripts/setup_venv.sh
```

The `source .venv/bin/activate` inside the script only applies within that script process. After it finishes, either activate manually:

```bash
source .venv/bin/activate
```

or run tools directly with the venv interpreter:

```bash
.venv/bin/python scripts/llm_ready.py
.venv/bin/python -m unittest tests.test_backends -v
```

## llm_ready.py

Checks configured LLM backends and reports READY/DOWN.

```bash
.venv/bin/python scripts/llm_ready.py
```

Requires `.env` variables such as `OLLAMA_BASE_URL`, `GROQ_API_KEY`, etc.

## install_ollama.sh

Installs/starts Ollama and pulls a model.

```bash
scripts/install_ollama.sh          # default qwen3.8:27b
scripts/install_ollama.sh small     # qwen3:8b
scripts/install_ollama.sh qwen3:8b  # explicit model
MODEL=qwen3:8b scripts/install_ollama.sh
```
