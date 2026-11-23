# Architecture

The harness is organized around typed objects and three state machines.
`GateState`, `RunState`, and `ToolState` own behavior; the append-only event log
is authoritative. State snapshots, projections, exports, replay, and scores are
derived from that log.

Detailed contracts live in [STATE_MACHINE_DESIGN.md](STATE_MACHINE_DESIGN.md);
the conceptual map is in [STATE_MACHINE_PRIMER.md](STATE_MACHINE_PRIMER.md).

## 1. Ownership model

```text
OPERATOR / ENTRY POINTS
  ./chaos ping|run|abort|state|replay|status|attach
  ./chaos cluster|target|scenario|probe|verify|nuke
  menu.sh / run_benchmark.sh
  db_cli.py / score.py / replay.py
            |
            v
COMPOSITION ROOT
  run_benchmark.py main()
    args and .env
    Scenario + validation
    topology, safety guard, capability, targets
    Backend, SQLiteStore, run_env, batch/run rows
            |
            v
RUN LIFECYCLE
  RunState + transition shell
    lease -> reset -> baseline -> inject -> fault check
    agent run -> verify -> FINISHED or ABORTED
    lease expiry -> INTERRUPTED -> resume triage or --abandon
            |
            v
AGENT COMPOSITION
  harness/agent.py Agent()
    ChatClient adapter + TOOL_REGISTRY + diagnosis/repair policy
  harness/loop.py AgentLoop.run(spec, ctx)
    messages, budget, LLM calls, tool calls, nudges, checks
            |
            v
BEHAVIOR OWNERS
  GateRuntime   diagnosis, nudges, repair proposals, typed outcomes
  GateState     authorization and gate path
  RunRuntime    lifecycle, lease, verification, terminal outcome
  RunState      folded run lifecycle state
  ToolLedger    folds canonical tool events per call
  ToolState     tool status and outcome per call
            |
            v
TYPED BOUNDARIES
  ExperimentSpec + RunContext
  Budget + Call + ToolOutcome
  LLMOutcome / LLMResponse / Verification
  Result / Approval / Refusal / Score
  Message / SystemPrompt / Journal / Checkpoint
            |
            v
INFRASTRUCTURE
  SQLiteStore, LLMClient, shell/fault scripts
  kubectl/etcdctl/ssh tools
  Reporter / LiveView
            |
            v
PERSISTENCE AND OUTPUTS
  SQLite append-only event log = source of truth
  state_json / state_snapshots / derived exports = projections
  replay.html / score summaries = read-only views
```

## 2. Main ownership map

| Concern | Owner |
|---|---|
| Scenario definition and validation | `tools/scenario.py`, `tools/scenario_schema.py` |
| Cluster identity and topology | `tools/topology.py` |
| Destructive safety | `tools/safety.py` |
| Backend configuration and fallback provenance | `llm/client.py`, `llm/backends.py`, `llm/registry.py` |
| Chat messages and redaction | `harness/message.py` |
| Event and artifact channel | `harness/journal.py` |
| Prompt policy | `harness/prompt.py` |
| Tool catalog and dynamic schemas | `tools/tool.py` |
| Gate behavior | `GateRuntime` and `GateState` |
| Run lifecycle, lease, resume, terminal outcome | `RunRuntime` and `RunState` |
| Tool status and outcome | `ToolLedger` and `ToolState` |
| Agent orchestration | `harness/loop.py` |
| Checkpointing and lease renewal | `harness/checkpoint.py` |
| Verification | `harness/verification.py` |
| Persistence | `storage/sqlite_store.py` |
| Scoring | `common/scoring.py` with the single canonical scorer |

Domain and application code receives typed inputs at composition boundaries.
Infrastructure writes events and projections through the store; machines never
call infrastructure directly.

## 3. Run flow

```text
./chaos run --scenario <id> --backend <name>
        |
        v
chaos_cli.main()
  build launch_id, argv, and environment
  start or attach to run_benchmark.py
        |
        v
run_benchmark.main()
  load scenario, validate it, freeze topology
  run destructive guard, capability checks, target resolution
  choose backend chain, build run_env, create batch/run rows
        |
        v
RunState fresh path
  acquire lease
  reset -> baseline -> inject -> wait_for_fault
  no observed fault -> ABORTED(fault_not_observed)
        |
        v
AgentLoop
  restore or create messages and state
  render topology-aware prompt
  call LLM under budget
  apply diagnosis and tool outcomes through GateState and ToolState
  fold nudge and repair events into gate path
        |
        v
RunState finish path
  AGENT_DONE -> VERIFYING
  VERIFY_OK or VERIFY_FAILED -> FINISHED
  write score and final record, release lease
  export derived artifacts after terminal state
        |
        v
read-only views
  score.py --db ...
  ./chaos replay
  chaos status / attach
```

The loop coordinates messages, budgets, LLM calls, and tool calls. Gate
authorization, run transitions, and tool outcomes are decided by their machines.

## 4. Resume path

```text
--resume / --resume-of
        |
        v
select candidate run: status in {running, interrupted}
        |
        v
RunState triage from previous_state
  topology mismatch        -> refuse
  unsafe setup/inject      -> new run after reset
  config_hash mismatch     -> retry unless --force-resume
  unknown mutating effect  -> abort and retry
  scenario/backend mismatch-> skip candidate
  read-only calls          -> SUPERSEDED
        |
        +-- safe -> re-lease, fold gate and run state,
                    restore messages, continue loop, verify, finish
```

`INTERRUPTED` is a parked, resumable state with a reason and `previous_state`.
The reaper observes expired leases and writes `run.lease_expired` exactly once
per observation key. `--abandon` is the explicit operator exit from
`INTERRUPTED`.

## 5. Persistence and projections

- `trace_events` is the append-only source of truth for machine transitions.
- `fold()` rebuilds `GateState`, `RunState`, and `ToolState` from ordered events.
- The persistence shell appends events and writes projections in one
  transaction; effects run after the transaction.
- `runs.status`, `runs.phase`, `runs.end_reason`, lease fields, gate scalars,
  tool status/outcome fields, `state_json`, and `state_snapshots` are projections.
- Caches can be dropped and rebuilt from the event log.
- Derived JSONL traces, replay HTML, and score summaries are read-only outputs.
- UI and CLI readers never derive authority from scalar defaults.

## 6. Scoring

One canonical scorer, `gate-path-v1`, reads folded `GatePath`, `RunState`, and
`ToolState` values. `strict_loop_success` is the only headline metric. Funnel
counts are views over the same path. Resumed, interactive, provider-error,
unsupported, and unverified-fault runs follow the canonical exclusion rules.

Fallback-enabled headline rates are grouped by `chain_id`. Per-backend baselines
use `CHAOS_LLM_FALLBACK=0`.

## 7. Safety and operational cautions

### 7.1 Destructive guard

`tools/safety.py` enforces context, cluster, and topology allow-lists before any
destructive action. Targets are allow-list validated before entering `run_env`,
and scenario commands quote target variables. New scenario commands must keep
that quoting pattern.

### 7.2 Fixture topology

`CHAOS_TOPOLOGY_JSON` is for tests and offline inspection. Normal runs refuse
fixture topology unless the explicit test-only opt-in is set. The destructive
identity and topology guard still applies.

### 7.3 Topology epoch and state cleanup

A topology hash mismatch refuses resume and requires an explicit discard for
intentional cleanup. Read-only status, attach, and show commands never delete
state. Abort active runs before discard.

### 7.4 Redaction and receipts

Messages, events, artifacts, derived traces, exports, and UI output redact
sensitive payloads at write time. Never put secrets in traces or commits.
Gate receipts are derived from `GateState` and `run_id`; they are never accepted
from tool arguments or stored in event payloads.

### 7.5 Disk-full exclusion

`etcd-disk-full` is excluded from live sweeps. Filling an etcd data filesystem
can leave a member unrecoverable and require host-level cleanup outside the
harness repair contract.

### 7.6 Live validation

Live or destructive validation requires operator confirmation. Non-destructive
smoke checks and offline suites run without that confirmation.

## 8. Observability

Replay and live views show machine states from folded events, with compact
transition descriptions and full evidence in artifacts. `db_cli.py doctor
--strict` checks terminal reasons, `FINISHED` invariants, expired leases,
illegal transitions, gate/projection agreement, unknown mutating outcomes,
duplicate generated transitions, interrupted-run metadata, and cache/fold
agreement.

## 9. Extensibility boundaries

New behavior belongs in typed events and transition tables. Add a fourth machine
only when the three existing machines cannot own the behavior: reset ladders,
topology epochs, LLM budgets, exports, and health observations do not need one.
Cluster-health observation stays outside authorization and never reaches the
model.
