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

## Run-store helpers

The authoritative run store and score table live at the repository root:

```bash
python3 db_cli.py init
python3 db_cli.py import-legacy
python3 db_cli.py doctor --strict
python3 score.py --db results.sqlite3 --format md
```

`llm_ready.py` checks configured live backends and exits non-zero if none are
ready; offline SQLite tests do not require a live backend. Live backend unit
smoke tests are opt-in with `CHAOS_LIVE_BACKEND_TESTS=1`.
