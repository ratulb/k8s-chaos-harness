# Architecture

This file is the maintained description of the current system. `README.md` is the
full operator guide. [STATE_MACHINE.md](STATE_MACHINE.md) is the generated state
reference; edit its generator or the machine tables, not the Markdown file. Code
is executable truth: if a document disagrees with the implementation, fix the
document.

## 1. What the harness is

The harness is a reproducible benchmark for operational reasoning on a real
Kubernetes cluster. It injects harness-owned etcd faults, gives the model
read-only diagnostics plus gated repair tools, verifies the final cluster state,
and scores the run from an append-only event log.

The benchmark asks one narrow question: can a model identify the root cause from
live evidence and choose an acceptable, safe repair? Diagnosis and repair are
separate signals. A correct guess is not a safe repair, and a failed repair can
still produce an educational trace. Triggering external remediation workflows is
out of scope.

The active SQLite database is the durable operational artifact and the only
input to headline scoring. JSONL exports, trace files, replay HTML, score
summaries, and UI streams are derived outputs.

### Scope

- Five injected etcd fault classes and one healthy control.
- Runtime-discovered Kubernetes clusters; three etcd members are recommended but
  not required.
- Self-hosted Ollama-compatible and hosted OpenAI-compatible backends, with
  explicit provenance for fallback chains.
- Direct named tool calls; no raw shell and no MCP transport in the benchmark.
- Diagnosis gate, policy-checked repairs, bounded budgets, and final verification.
- SQLite checkpoint/event store with safe resume and a reaper for crashed runs.
- Live/plain terminal views, read-only attach, static replay, and one canonical
  scorer.

Explicit non-goals include generic Kubernetes autopilot behavior, full Kubernetes
API fault emulation, model-visible fault injection, model-visible scoring truth,
and production remediation. Repair mode is best effort: it records attempted,
succeeded, failed, and unverifiable outcomes and never claims recovery without
verification.

## 2. System at a glance

```text
operator / CI
  -> ./chaos                         CLI, safety, launch/attach/abort/status
  -> run_benchmark.py                 composition root
       scenario -> topology -> guard -> capability -> target
       RunRuntime -> reset -> baseline -> inject -> fault check
       AgentLoop -> LLM -> GateRuntime / ToolRuntime
       verification -> finish -> score projection -> derived exports

infrastructure
  cluster/*.sh, faults/*.sh, verify/*.sh, kubectl, etcdctl, ssh
  LLM endpoint chain

persistence
  SQLite append-only trace_events = authority
  RunState / GateState / ToolState = folds over those events
  runs, state_json, snapshots, artifacts, scores = projections
  live UI, attach, replay, score.py = read-only views
```

The runner owns destructive setup: reset, baseline, injection, and final verify.
The model never sees fault scripts, truth labels, or scoring inputs. Tools expose
only named read-only diagnostics and gated repairs.

## 3. Ownership map

| Concern | Owner |
| --- | --- |
| Scenario definition and validation | `tools/scenario.py`, `tools/scenario_schema.py` |
| Cluster identity and topology | `tools/topology.py` |
| Destructive safety | `tools/safety.py` |
| Backend chain and responder provenance | `llm/client.py`, `llm/backends.py`, `llm/registry.py` |
| Chat messages and redaction | `harness/message.py` |
| Event and artifact channel | `harness/journal.py` |
| Prompt policy | `harness/prompt.py` |
| Tool catalog and schemas | `tools/tool.py` |
| Gate behavior | `GateRuntime` and `GateState` |
| Run lifecycle | `RunRuntime` and `RunState` |
| Tool status/outcome | `ToolLedger` and `ToolState` |
| Agent orchestration | `harness/loop.py` |
| Checkpointing and lease renewal | `harness/checkpoint.py` |
| Verification | `harness/verification.py` |
| Persistence and doctor | `storage/sqlite_store.py` |
| Scoring | `common/scoring.py` |
| Reaper and explicit abandon | `harness/reaper.py` |

Domain and application code receives typed inputs at composition boundaries.
Runtimes emit canonical events through `Journal`; the store appends events and
rebuilds projections from them. State machines do not call infrastructure.

## 4. State-machine model

Three always-on machines own behavior:

- `GateState` decides whether diagnosis is correct, whether repair is authorized,
  which repair was attempted, and which gate violations occurred.
- `RunState` owns status, phase, lease, verification, interruption, resume triage,
  and terminal outcome.
- `ToolState` owns each call's intended/started/finished/failed status and typed
  outcome.

The pure core in `harness/state_machine.py` provides transition lookup, folding,
validation, and rejection events. Runtimes in `harness/*_runtime.py` turn method
calls into canonical events. The tables in `harness/machines/` are the
exhaustive definition of legal transitions and are projected into
[STATE_MACHINE.md](STATE_MACHINE.md).

Canonical events use a nested envelope:

```json
{
  "type": "run.lease_expired",
  "payload": {
    "event": "lease_expired",
    "payload": { "previous_state": "leased", "lease_expires_at": 100.0 },
    "idempotency_key": "reap:run-1:leased:100.0"
  }
}
```

Runtimes deduplicate generated events by `idempotency_key`; folding deduplicates
by `seq`/key. Illegal transitions are recorded as `state_transition_rejected`
events and ignored by the emitting runtime. A duplicate generated key is a doctor
error, not silently accepted history.

Diagram labels are persisted values, not enum names. Gate states use wire values
such as `no_fault_cleared`. Run states appear as `status:phase`, such as
`running:baseline`. Tool diagrams collapse machine phases to persisted status;
`UNKNOWN_EFFECT` appears as `failed` with `result_code=unknown_effect`.

## 5. Run lifecycle

```text
create run -> lease -> reset -> baseline -> inject -> fault check -> agent
                                                            |
                                              agent done / terminal error
                                                            v
                                                        verify
                                                            v
                                  FINISHED or ABORTED -> score -> final record
```

Each run pins a scenario hash, topology hash, backend chain, prompt hash, and
provenance fields before destructive work. Reset failures and baseline failures
are infrastructure failures. An injection command failure is `injection_failed`;
a stimulus that was not proven present is `fault_not_observed`. Both are terminal
and excluded from headline scoring.

The final state writes terminal fields, recomputes the canonical score, writes a
final-record artifact, and releases the lease. Derived JSONL/trace/UI exports
happen only after the terminal state unless disabled.

### Resume, reaper, and abandon

A crashed run is parked in `INTERRUPTED` with `previous_state`,
`lease_expires_at`, and an end reason. The reaper runs on CLI invocation: it finds
expired leases with no live owner and emits `run.lease_expired` once per
observation key. It never resumes work from the dead process.

Resume triage is a lookup from `previous_state`:

- Early setup phases re-lease and resume.
- Inject/fault-check phases start a new run after reset.
- Agent phases resume unless a mutating call has an unknown effect, which aborts
  and retries.
- Verify phases rerun verification.

Topology mismatch always refuses resume. `--force-resume` bypasses only a config
hash mismatch. `./chaos abort <run_id> --abandon` is the explicit operator exit
from `INTERRUPTED` and writes `run.abandon`.

## 6. Agent surface

The model sees the system prompt, frozen topology, tool schemas, conversation
state, and truncated tool output. It does not see fault scripts, truth labels,
raw shell, SQLite, or scoring inputs.

Read-only diagnostics cover Kubernetes resources, logs, events, etcd health and
membership, alarms, host journals, containers, services, disk usage, and paths.
`submit_diagnosis` accepts one exact root-cause label and can succeed at most on
the first accepted attempt for strict scoring.

The gate receipt is derived from folded `GateState` and `run_id`. It is a
correctness handshake, not a secret or a client-supplied token. Prompt mode adds
operator confirmation and marks the run interactive; harness state remains
authoritative after resume.

Repair names are canonicalized before policy. Blocked calls produce typed policy
violations; forbidden calls also set `forbidden_reason`. Repair result strings are
evidence for operators, but state and score read typed fields.

## 7. Persistence and projections

SQLite is the active epoch. The tables have distinct roles:

| Tables | Role |
| --- | --- |
| `trace_events` | Append-only authority for machine transitions |
| `messages`, `llm_requests`, `tool_calls`, `run_artifacts` | Append-only evidence |
| `runs`, `batches`, `scenarios` | Run/control projections and pinned facts |
| `state_snapshots`, `ui_stream` | Durable snapshots and redacted UI stream |
| `diagnosis_attempts`, `policy_violations` | Gate history |
| `scores`, `score_reports` | Derived score projections |

`apply_machine_event()` appends an event, rebuilds state from the event log, and
writes projections. `runs.status`, `runs.phase`, `runs.end_reason`, lease fields,
gate scalars, tool status fields, `state_json`, and `state_snapshots` are all
rebuildable. Readers never derive authority from scalar defaults.

`db_cli.py doctor --strict` checks schema version, final records, score rows,
terminal reasons, expired leases with live owners, in-flight tools, unknown
mutating effects, stale sequence markers, stale caches, missing canonical
events, rejection events, duplicate generated keys, interrupted metadata, and
cache/fold drift.

Exports, replay HTML, and score reports are read-only outputs. `state discard`
deletes the active epoch and evidence; read-only commands and explicit resume
never do that.

## 8. Scoring

`common/scoring.py` is the single canonical scorer, `gate-path-v1`. It reads the
folded `GatePath`, run state, and tool states from `SQLiteStore.load_records()`.
Score rows are projections recomputed from the fold; the scorer is not a second
source of truth.

`strict_loop_success` is the headline metric. It requires an observed fault,
finished run, strict gate terminal, first-attempt correct diagnosis, receipt,
passing verification, no violations, `end_reason=verify_ok`, acceptable repair or
no repair for `no_fault`, and no exclusion reason. `loop_success` is historical
only and must not be quoted as the current headline.

Excluded groups include resumed, interactive, unsupported, provider-error,
unverified-fault, budget cutoff, abandoned, and illegal-transition runs. A
fallback answer is headline-eligible; earlier failed candidates are not a
run-level provider error. `runs.backend`/`runs.model` keep the configured chain
identity, while the responding backend and attempted path are recorded in
`llm_requests`, messages, artifacts, and assistant events. Per-backend baselines
pin one endpoint with `CHAOS_LLM_FALLBACK=0`. Temperature is fixed at `0.2`;
`--seed` is metadata.

## 9. Topology and safety contract

A live reachable cluster is the source of truth. `setup.conf` is a rebuild recipe
and fallback host list, never an override for live discovery. `CHAOS_TOPOLOGY_JSON`
is for tests and offline inspection; normal runs refuse fixture topology unless
the explicit test-only opt-in is set.

The topology hash includes sorted control-plane names/IPs, etcd member identity,
member count, and probe node. It excludes health, Ready state, transient faults,
and scores, so recovery alone does not create a new epoch. Quorum is
`floor(N / 2) + 1`; selectors resolve against the frozen topology.

`tools/safety.py` enforces context, cluster, and topology allow-lists before
destructive actions. A normal run with a changed topology archives the old epoch
and exits; `--discard-stale` is required for intentional deletion. Resume refuses
a topology change. `./chaos status`, attach, and target show are read-only and
warn instead of deleting.

Messages, events, artifacts, exports, UI chunks, and state caches are redacted at
write time. Gate receipts are derived and stripped from persisted caches. Never
put secrets in traces or commits. `etcd-disk-full` remains excluded from live
sweeps because filling an etcd data filesystem can require host-level recovery.

## 10. Operator surface

`./chaos` is the intended entry point. It provides `ping`, `status`, `target`,
`scenario`, `run`, `attach`, `abort` (including `--abandon`), `state`, `cluster`,
and `replay`. Lower-level tools are `db_cli.py`, `score.py`, `replay.py`, and
`run_benchmark.py`. The generated `menu.sh` calls `./chaos run`.

The live view and `attach` are read-only followers of the SQLite event and UI
streams. Detached runs record `launch_id` and `pid`. Destructive commands require
explicit confirmation. `./chaos doctor`, `./chaos score`, `./chaos export`, and
`score.py --reference-only` are not implemented; use the lower-level commands.

See `README.md` for quickstart, command flags, backend configuration, scenario
tables, troubleshooting, and safety examples.

## 11. Verification

Offline verification for changes:

```bash
python3 scripts/gen_state_diagrams.py --output docs/STATE_MACHINE.md
python3 -m compileall -q .
bash -n run_benchmark.sh
python3 -m unittest discover -s tests -q
git diff --check
```

Live validation requires operator approval. Non-destructive smoke checks may run
without destructive approval; fault injection, repair calibration, and full
sweeps do not.

## 12. Status and roadmap

Completed:

- live topology discovery and `chaos cluster nuke` acceptance on the lab cluster
- all six scenarios and dynamic quorum target selection
- diagnosis gate, gated repairs, budgets, and streamed provenance
- SQLite event authority, safe resume, reaper, `--abandon`, and doctor checks
- three state machines, generated transition reference, and one canonical scorer
- static replay, score reports, live/plain UI, attach, abort, and prompt approval
- live fault-present calibration for all five injected scenarios
- a five-run post-hardening pilot: 100% diagnosis, 80% verify, 20% strict

Open or deferred:

- fresh cross-backend sweep after the state-machine hardening
- refreshed replay and score artifacts for the active epoch
- fault-injection determinism work
- candidate extra scenario: rebuild with prior etcd state
- blog post and demo notes
- repair-mode statuses beyond the current best-effort behavior
- `./chaos doctor`, `./chaos score`, `./chaos export`, and `--reference-only`

Historical results remain in git history and derived artifacts, not in the
maintained documents. New status belongs in this file; new machine contracts
belong in the generated state reference or its source tables.
