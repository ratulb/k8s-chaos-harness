# Architecture

This document explains the parts of the harness, what each part owns, and how they work together during one run. It is written for someone who needs to understand the system before operating or changing it.

Start here:

- [`README.md`](../README.md) for commands, configuration, safety, and troubleshooting.
- [`call_flow.md`](call_flow.md) for one worked run, call by call.
- [`STATE_MACHINE.md`](STATE_MACHINE.md) for generated state and transition reference.

Read sections 1-4 first. They give the mental model needed for the rest.

## 1. What this system is

This harness asks a narrow question:

> Given a real Kubernetes control plane with a broken etcd member, can a model find the root cause from read-only evidence and choose an acceptable, safe repair?

The harness, not the model, owns the dangerous parts of the experiment. It resets the cluster, establishes a baseline, injects a known etcd fault, proves the fault is present, runs the model against a constrained tool surface, verifies the final cluster state, and scores the result.

Diagnosis and repair are separate signals. A correct diagnosis does not authorize any repair. A useful run can fail the headline metric if it guesses right but proposes an unsafe repair.

This is an educational benchmark, not a production remediation service, a generic Kubernetes autopilot, or a model-visible fault injector. External remediation workflows are out of scope. Repair mode is best effort: it changes prompts and reporting, but still resets, verifies a baseline, and injects.

## 2. The participants

```text
operator / CI
    |
    v
./chaos  ---------  run_benchmark.py
                        |
        +---------------+---------------+
        |               |               |
   Kubernetes/etcd    LLM backend    results.sqlite3
        |               |               |
   cluster scripts   tool schemas    events, state, scores
                        |               |
                   named tools ------> live UI / replay / score.py
```

| Participant | Owns | Does not own |
| --- | --- | --- |
| Operator / CI | Choosing scenario, backend, guard, and when to run | Cluster repair decisions |
| `./chaos` (`chaos_cli.py`) | Parsing commands, loading `.env`, launching the runner, reaping expired leases | Scenario policy or scoring |
| `run_benchmark.py` | Wiring the whole run: scenario, topology, guard, store, runner, agent, verification, exports | The truth label or model answer |
| Kubernetes / etcd | The live substrate that is reset, broken, inspected, and verified | The benchmark's definition of success |
| LLM backend | Producing messages and named tool calls | Injecting faults, seeing truth, bypassing policy |
| `results.sqlite3` | Durable evidence and authoritative event log | Producing a diagnosis itself |
| Reports and views | Reading stored evidence to show status, scores, and replay | Changing run state |

The important boundary: the model receives a narrow prompt, frozen topology facts, tool schemas, and conversation history. It does not receive the scenario id, root-cause truth, fault scripts, injection commands, repair policy, score formula, or raw shell access.

## 3. Terms used in this document

| Term | Meaning in this system |
| --- | --- |
| Scenario | A JSON file that defines the fault, hidden root cause, target rules, acceptable and forbidden repairs, commands, and budgets. |
| Topology hash | A stable fingerprint of the discovered control planes, etcd membership, member count, and probe node. Health and transient faults are excluded. |
| Epoch | The active `results.sqlite3` associated with one topology identity. A topology change starts a new epoch by archiving the old one. |
| Event | An append-only record in `trace_events`, such as `tool.success` or `verify_ok`. Events are the authority for state and scoring. |
| Fold | Replaying events through a state machine to reconstruct current state. It is how a crashed run becomes understandable. |
| Projection | A stored or exported view rebuilt from events, such as `runs.status`, `state_json`, `scores`, or replay HTML. |
| Gate | The diagnosis and repair authorization path. It opens only after an accepted diagnosis. |
| Receipt | A derived marker, `approved:{run_id}`, proving the gate accepted a diagnosis. It is not a secret or human authorization. |
| Truth | The scenario's hidden `root_cause`. It is stored for scoring and never shown to the model. |
| Responder | The backend that actually answered. It is recorded separately from the configured backend chain. |
| Headline score | `strict_loop_success`, the current end-to-end cleanliness metric. |

## 4. One run, in order

The detailed call-by-call version is in [`call_flow.md`](call_flow.md). The short version:

1. `./chaos run` loads `.env`, calls the reaper, and launches `run_benchmark.py`.
2. The scenario is loaded and validated before any side effect.
3. Topology is discovered and frozen.
4. The destructive guard checks the cluster context or cluster id; the topology hash is checked if an allow-list is configured.
5. Capability checks and target resolution decide eligible nodes and quorum behavior.
6. A stale epoch is archived and the run exits unless `--discard-stale` is intentional.
7. The run is created, leased, and moved through reset, baseline, injection, and fault observation.
8. The agent loop begins only after the harness has proven the fault is present.
9. The model calls named diagnostic tools, submits a diagnosis, and may propose a repair.
10. The gate checks diagnosis correctness, approval, repair canonicalization, forbidden repairs, and acceptable repairs.
11. Final verification checks etcd health and that the injected fault is cleared.
12. The run finishes, the canonical scorer computes a score, the lease is released, and derived exports are written.

A healthy LLM backend is a prerequisite for the run, but it is not permission to touch the cluster. That permission comes from the destructive guard.

## 5. Scenario contract and safety

Each scenario JSON file is the contract for one experiment:

| Field group | What it controls |
| --- | --- |
| `root_cause` | The hidden truth label. |
| `inject` / `verify` | The harness-owned fault command and health command. |
| `fault_present_check` | How the harness proves the stimulus actually happened. |
| `target_nodes`, selectors, quorum rules | Which discovered nodes may be affected. |
| `acceptable_repairs`, `forbidden_repairs` | The repair policy enforced by the gate. |
| `timeout_seconds`, budgets | How long setup and agent work may run. |
| `risk`, `impact_class` | Operator-facing classification and capability checks. |

Safety works in layers:

- The destructive guard in `tools/safety.py` requires an allow-listed current context or cluster id. `CHAOS_ALLOWED_TOPOLOGY_HASHES` is optional but strongly recommended.
- `./chaos run` is guarded by configuration, not an interactive prompt. Confirmation exists for `abort all`, `state discard`, and `cluster nuke`.
- Fixture topology is rejected for live runs unless `CHAOS_ALLOW_FIXTURE_RUNS=1`; fixtures are for offline tests.
- Topology mismatch refuses resume. A fresh run archives the old epoch and exits unless `--discard-stale` is passed.
- `setup.conf` is external lab configuration at `$K8S_EASY_INSTALL/setup.conf`, defaulting to `~/k8s-easy-install/setup.conf`. It is a rebuild recipe and fallback host list, not an override for live discovery.
- `etcd-disk-full` is excluded from live sweeps by operator convention. Code and `menu.sh` still include it, so the operator must skip it deliberately.

## 6. What the model sees and may do

The model gets a deliberately small view of the system:

| Visible to the model | Kept outside the prompt |
| --- | --- |
| Control-plane node names and probe node | Scenario id and `root_cause` truth |
| Quorum sentence derived from frozen topology | Reset, baseline, inject, and verify commands |
| Six allowed diagnosis labels | Fault-present and fault-cleared predicates |
| Tool names, parameters, and descriptions | Acceptable and forbidden repair lists |
| Prior assistant, tool, and nudge messages | Gate phase, receipt, budget counters, score formula |

All model-visible operations are named tools. There is no raw shell and no MCP transport in the benchmark. Read-only diagnostics cover Kubernetes resources, logs, events, etcd health and membership, alarms, host journals, containers, services, disk usage, and paths.

The gate order is:

```text
diagnosis submitted
  -> canonical label accepted within the two-attempt cap
  -> harness-side approval active
  -> repair name canonicalized
  -> repair not forbidden
  -> repair acceptable
  -> repair executed
```

A blocked repair is a recorded policy violation, not a prompt suggestion. `harness/tooling.py` enforces the six `BLOCKED` conditions. In autonomous mode the receipt is a correctness handshake, not a secret. Prompt mode adds operator confirmation and marks the run interactive; harness state remains authoritative after resume.

## 7. Where evidence lives

SQLite is the active epoch and the authority. Event tables are the truth; scalar columns, caches, scores, exports, and replay are rebuildable views.

The core chain is:

```text
tool or agent action
  -> Journal.event()
  -> trace_events append
  -> rebuild_state() folds gate/run/tool machines
  -> GatePath + RunState + ToolState
  -> scores / state_json / live UI / replay
```

| Table group | Role |
| --- | --- |
| `messages`, `trace_events`, `llm_requests`, `tool_calls`, `run_artifacts` | Append-only evidence |
| `runs`, `batches`, `scenarios` | Run and control projections, including pinned hashes |
| `state_snapshots`, `ui_stream` | Durable snapshots and redacted UI chunks |
| `diagnosis_attempts`, `policy_violations` | Gate history |
| `scores`, `score_reports` | Derived score projections |

`runs.state_json` is a cache, not a second source of truth. `get_state()` rebuilds from events and writes a snapshot. `db_cli.py doctor --strict` checks drift between authoritative events and projections.

Derived files are useful but not authoritative:

| File | Role |
| --- | --- |
| `runs.jsonl` | Legacy export |
| `traces/*.jsonl` | Per-run traces, snapshots, and UI artifacts |
| `logs/*.log` | Runner and launcher output |
| `replay.html` | Redacted static view |

`quant` and `prompt_hash` exist for schema and import compatibility; live runs leave them unset. Live provenance fields are `model`, `temperature`, `seed`, and `harness_git_sha`.

## 8. Three state machines, in operator language

Three machines fold the event log. They do not call infrastructure; runtimes translate method calls into canonical events.

| Machine | What it answers | Generated reference |
| --- | --- | --- |
| Gate | Was the diagnosis accepted, is repair allowed, and did a violation occur? | [`STATE_MACHINE.md`](STATE_MACHINE.md) |
| Run | What phase is the run in, is it terminal, and what happens after a crash? | [`STATE_MACHINE.md`](STATE_MACHINE.md) |
| Tool | Did each call succeed, fail, get denied, or become unsafe to replay? | [`STATE_MACHINE.md`](STATE_MACHINE.md) |

Run phases are persisted as `status:phase`. For example, machine phase `baselining` appears as `running:baseline`. Tool phases collapse into persisted statuses: success and supersede both appear as `finished`; unknown effect appears as `failed` with `result_code=unknown_effect`.

A crashed run is parked in `interrupted:interrupted` with `previous_state`. The reaper runs on CLI startup and records expired leases once per observation key. Resume triage is a lookup from `previous_state`:

| Previous phase | Action |
| --- | --- |
| `pending`, `leased`, `resetting`, `baselining` | Re-lease and resume |
| `injecting`, `fault_check` | Create a new run after reset |
| `agent_running` | Resume; abort and retry if a mutating tool has unknown effect |
| `verifying` | Rerun verification |

Topology mismatch always refuses resume. `--force-resume` bypasses only a config hash mismatch. `./chaos abort <run_id> --abandon` is the explicit exit from `INTERRUPTED`.

## 9. How scoring works

`common/scoring.py` is the only scorer, version `gate-path-v1`. It reads folded `GatePath`, run state, and tool states. `loop_success` is historical only; `strict_loop_success` is the headline.

`strict_loop_success=1` requires:

| Requirement | Where it comes from |
| --- | --- |
| Fault was observed | Folded run state from `fault_observed` |
| Run is `finished:finished` | Folded run state |
| Gate terminal is `repaired` or `no_fault_cleared` | Folded `GatePath` |
| Diagnosis was correct on the first attempt | Folded gate path |
| Receipt was issued | Folded gate path |
| Verification passed | Folded run state |
| No gate or tool policy violations | Folded gate path and tool states |
| Folded end reason is `verify_ok` | Folded run state, not raw `runs.end_reason` |
| Acceptable repair, or no repair for `no_fault` | Gate path and scenario policy |
| No exclusion reason | Scorer |

A stored `runs.end_reason` of `budget_tokens` can still coexist with `strict_loop_success=1` when final verification changes the folded reason to `verify_ok`. This is intentional: scoring reads the fold.

Fallback is provenance, not a score gate. `runs.backend` and `runs.model` keep the configured chain identity. The responding backend, requested backend, skipped candidates, and fallback path are recorded in `llm_requests`, messages, `llm_response_full`, and assistant events. For clean per-backend baselines, pin one endpoint with `CHAOS_LLM_FALLBACK=0`.

Benchmark turns use `temperature=0.2`. `--seed` is metadata, not sampling control.

## 10. Failure playbook

| Symptom | Meaning | Next place to look |
| --- | --- | --- |
| `reset_failed`, `baseline_failed` | Cluster was not clean before injection; infrastructure failure | Runner logs, reset ladder, cluster health |
| `injection_failed` | Fault command failed | Injection artifact, target resolution |
| `fault_not_observed` | Stimulus was not proven present | Fault-check predicate, scenario timeout |
| `BLOCKED: ...` | Gate rejected a repair before execution | `harness/tooling.py`, policy violations |
| `provider_error_*` | Every configured candidate failed | LLM request events and error artifact |
| `INTERRUPTED` | Lease expired or reaper parked the run | `previous_state`, resume triage, `attach` |
| `verify_ok=false` | Health check failed or fault was not cleared | Verify artifact and fault-cleared check |
| Stale topology message | Active epoch belongs to another topology identity | `./chaos target show --json`, archive directory |
| Doctor drift | Cache or projection disagrees with events | `db_cli.py doctor --strict`, trace events |

## 11. Code map for debugging

| Concern | Primary files |
| --- | --- |
| CLI and launch | `chaos_cli.py`, `chaos` |
| Composition root | `run_benchmark.py` |
| Scenario definition and validation | `scenarios/*.json`, `tools/scenario.py`, `tools/scenario_schema.py` |
| Topology, quorum, targets, hashes | `tools/topology.py` |
| Destructive guard | `tools/safety.py` |
| LLM chain and provider calls | `llm/registry.py`, `llm/client.py`, `llm/response.py` |
| Prompt and tool schemas | `harness/prompt.py`, `tools/tool.py`, `common/vocab.py` |
| Agent loop | `harness/loop.py`, `harness/budget.py` |
| Diagnosis and repair enforcement | `harness/tooling.py`, `harness/call.py` |
| Gate, run, and tool machines | `harness/machines/`, `harness/*_runtime.py` |
| Event and artifact channel | `harness/journal.py`, `harness/message.py`, `harness/reporter.py` |
| Checkpointing and reaper | `harness/checkpoint.py`, `harness/reaper.py` |
| Cluster setup, injection, verification | `cluster/*.sh`, `faults/*.sh`, `tools/fault_checks.py`, `harness/verification.py` |
| Persistence and doctor | `storage/sqlite_store.py`, `db_cli.py` |
| Scoring and reports | `common/scoring.py`, `score.py`, `replay.py` |
| Generated state reference | `scripts/gen_state_diagrams.py`, `docs/STATE_MACHINE.md` |

## 12. Verifying a change

Offline checks:

```bash
python3 scripts/gen_state_diagrams.py --output docs/STATE_MACHINE.md
python3 -m compileall -q .
bash -n run_benchmark.sh
python3 -m unittest discover -s tests -q
git diff --check
```

Live validation requires an explicitly disposable cluster and fresh guard values. Do not run destructive scenarios during ordinary documentation or code checks.

## 13. Current status

Implementation status, limitations, historical results, and roadmap belong in [`README.md` §17](../README.md). This document describes the system as implemented and does not maintain result tables or release status.
