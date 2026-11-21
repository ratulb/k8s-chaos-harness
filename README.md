# k8s-chaos-harness

Reproducible etcd fault diagnosis and repair benchmark on real Kubernetes
clusters.

- `DESIGN.md`: canonical design and locked decisions.
- `PHASES.md`: current status and action items.
- `SQLite.md`, `chaos_ui.txt`, `feedback.md`, `harness-cli.tgz`: historical
  reviewer input.

## Cluster model

The harness discovers control planes, etcd members, IPs, roles, and quorum at
runtime. It does not require fixed hostnames, IPs, SSH users, or exactly three
nodes.

Three etcd members remain the loud recommendation. Even counts and 1-2 member
clusters are supported with warnings and best-effort behavior.

Offline topology fixtures can be inspected without a cluster:

```bash
CHAOS_TOPOLOGY_JSON=tests/fixtures/topologies/n5.json ./chaos target show --json
```

A live cluster reachable through the current kubeconfig is the source of truth.
An existing cluster does not need to be rebuilt.

For a clean disposable lab, the recommended path is `k8s-easy-install` driven by
a populated `setup.conf`. Override its location with:

```bash
export K8S_EASY_INSTALL=/path/to/k8s-easy-install
```

Useful lower-level cluster commands:

```bash
bash cluster/reset_cluster.sh            # full nuke/rebuild + verify + snapshot
bash cluster/snapshot_clean_state.sh     # save clean etcd static-pod manifests
bash cluster/restore_clean_state.sh      # fast restore after a fault
bash cluster/verify_etcd_healthy.sh      # baseline / final verify
```

## Backends

One backend is selected per run; there is no automatic fallback chain.

- `ollama`: self-hosted Ollama-compatible endpoint, local or tunneled.
- `groq`
- `openrouter`
- `cloudflare`

A Kaggle notebook can be used as optional temporary infrastructure for an
Ollama endpoint, but `kaggle` is not a backend name.

Copy `.env.example` to `.env` and configure at least one backend.

## Recommended command surface

Phase 9 introduces a repo-local `./chaos` command. Use the lower-level commands
in the next section for anything not exposed by `./chaos` yet.

```text
./chaos doctor
./chaos target show
./chaos scenario list
./chaos scenario show <id>
./chaos scenario validate
./chaos scenario menu --output menu.sh
./chaos run --scenario <id> --backend ollama --runs 1 --ui live
./chaos run --scenario <id> --backend ollama --detach --ui plain
./chaos attach <run_id>
./chaos attach <run_id> --follow
./chaos abort <run_id|all> [--no-kill]
./chaos status
./chaos state discard [--yes]
./chaos cluster nuke [--yes]
./chaos score [--format md] [--reference-only]
./chaos export
./chaos replay [--output replay.html] [--include-interactive]
```

Important behavior:

- `chaos target show` reports dynamic topology and warns loudly when the cluster
  is not the recommended 3-member topology.
- `chaos run` is a new attempt. If live topology changed, it discards stale
  SQLite operational state loudly and starts fresh.
- `chaos run --resume-of <run_id>` is an explicit resume. It refuses when
  topology changed and never discards.
- `chaos run --approve prompt` asks the operator before a correct diagnosis may
  grant repair approval. It stops the live view while prompting, suppresses the
  token in UI/trace output, and records `interactive=1`; headline scoring skips it
  unless `--include-interactive` is used.
- `chaos run --detach` prints `launch_id`, `pid`, and a log path. The run row
  records `launch_id` and `pid`, and child output goes to
  `logs/<launch_id>.log`.
- `chaos attach <run_id> [--follow]` is read-only and consumes the same
  SQLite `trace_events` and `ui_stream` rows as the live view.
- `chaos abort <run_id|all>` sends `SIGTERM` to live PIDs unless `--no-kill`,
  then records a terminal `aborted` status and operator event. `all` requires
  confirmation or `--yes`.
- `chaos replay` builds a dependency-free static HTML replay from SQLite events
  plus a results matrix. Text is redacted before rendering; interactive runs stay
  in the replay but are excluded from the matrix unless `--include-interactive`.
- `chaos state discard` permanently removes the active operational epoch. There
  is no archive.
- `chaos cluster nuke` rebuilds from `setup.conf`, accepts whatever live
  topology results, and discards stale state only after a successful rebuild.
  Live acceptance passed on 2026-11-21. A reduced 13-run cross-backend sweep
  also completed; the cluster was rebuilt clean afterwards.

## Current lower-level commands

Initialize and inspect SQLite:

```bash
python3 db_cli.py init
python3 db_cli.py doctor --strict
python3 db_cli.py status
```

Legacy JSONL import is explicit migration only:

```bash
python3 db_cli.py import-legacy
```

Run and score directly:

```bash
python3 run_benchmark.py --scenario etcd-member-down --backend ollama --runs 1
python3 score.py --db results.sqlite3 --format md
```

A repair call left with an unknown side effect aborts the original run and
creates a fresh retry linked by `resume_of`. Resumed runs are excluded from
headline rates by default.

Scenarios:

- `control-healthy-noisy`
- `etcd-disk-full`
- `etcd-member-down`
- `etcd-member-wiped`
- `etcd-quorum-loss`
- `etcd-wrong-peer-url`

Each run records messages, tool intents/results, LLM requests, full artifacts,
score fields, topology identity, and a final record in SQLite. JSONL and trace
files are regenerated exports.

Repairs are gated: the agent must submit an exact diagnosis label before any
repair tool is authorized, and forbidden repairs are blocked and logged.

## SQLite location

SQLite is authoritative for the active epoch. Set `CHECKPOINT_DB` if the default
path is unsuitable.

```bash
export CHECKPOINT_DB=/data/k8s-chaos-harness/results.sqlite3
```

`runs.jsonl` and `traces/*.jsonl` are derived files. Do not edit them as source
of truth.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

If the configured `OLLAMA_MODEL` is not present locally, override it for the
smoke test:

```bash
OLLAMA_MODEL=qwen3:8b python3 -m unittest discover -s tests -v
```

Live backend smoke tests are opt-in:

```bash
CHAOS_LIVE_BACKEND_TESTS=1 python3 -m unittest tests.test_backends -v
```
