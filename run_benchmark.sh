#!/usr/bin/env bash
# One-command benchmark wrapper: DB init, run, then score.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB="${CHECKPOINT_DB:-$ROOT/results.sqlite3}"

prev=""
for arg in "$@"; do
  if [[ "$prev" == "--db" ]]; then
    DB="$arg"
  fi
  if [[ "$arg" == --db=* ]]; then
    DB="${arg#--db=}"
  fi
  prev="$arg"
done

python3 "$ROOT/db_cli.py" --db "$DB" init
python3 "$ROOT/run_benchmark.py" --db "$DB" "$@"
python3 "$ROOT/score.py" --db "$DB" --format md
