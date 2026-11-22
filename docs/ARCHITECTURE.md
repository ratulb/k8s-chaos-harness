# Architecture draft (reverse-engineered)

Status: draft, reverse-engineered from the implementation at review baseline
commit `1952bb2`. Line references are relative to the repository root and reflect
that commit; current code wins.

This document captures:

- the architecture that exists today;
- the object/model boundaries we have introduced so far;
- how a scenario flows from operator start to final output;
- the resume path;
- design gaps found by tracing the whole system;
- a candidate target architecture and refactor sequence.

## 1. Current architecture, as built

```text
OPERATOR / ENTRY POINTS
  ./chaos run ...            chaos_cli.py:201
  menu.sh / run_benchmark.sh
  db_cli.py / score.py / replay.py / chaos status|attach|replay
            |
            v
COMPOSITION ROOT (real one)
  run_benchmark.py:355  main()
    - args + .env
    - Scenario + ValidationReport
    - Topology / safety guard / capability / targets
    - Backend
    - SQLiteStore
    - run_env + config_hash
    - batch/run rows
            |
            v
EXPERIMENT SETUP / RESUME
  run_benchmark.py:111  _run_fresh()
    reset -> baseline -> inject -> wait_for_fault
  run_benchmark.py:243  _run_resume()
    restore lease/state/messages; no reset/inject
            |
            v
AGENT COMPOSITION
  harness/agent.py  Agent() defaults / build_agent() shim
    defaults: ChatClient adapter + TOOL_REGISTRY + Diagnosis + Repair
  harness/agent.py  run_scenario() legacy dict facade
            |
            v
TYPED BOUNDARY
  ExperimentSpec + RunContext
  Budget + Call/CallExecution + LLMOutcome/LLMResponse + Verification
  Result/Approval/Refusal
            |
            v
AGENT ORCHESTRATION
  harness/loop.py  AgentLoop.run(spec, ctx) -> RunResult
    - Journal owns events/artifacts/UI emission
    - Checkpoint captures AgentState into RunStore
    - Message[] conversation
    - LLMOutcome owns LLM success/failure and commits the audit trail
    - CallExecution commits tool intent and results
    - Verification commits verification evidence
    - record + Score + finish_run
            |
            v
DOMAIN / POLICY OBJECTS
  Scenario              tools/scenario.py
  ExperimentSpec        harness/spec.py
  RunContext            harness/spec.py
  Budget / Call         harness/budget.py / harness/call.py
  CallExecution         harness/call.py
  Checkpoint            harness/checkpoint.py
  Journal               harness/journal.py
  Verification          harness/verification.py
  LLMOutcome/LLMResponse llm/response.py
  Result/Approval/Refusal common/results.py
  LLMError / Score      common/errors.py / common/scoring.py
  SystemPrompt          harness/prompt.py
  Message               harness/message.py
  Tool / Registry       tools/tool.py
  ToolOutcome           tools/tool.py
  AgentState            harness/tooling.py
  Diagnosis / Repair    harness/tooling.py
            |
            v
INFRASTRUCTURE / ADAPTERS
  SQLiteStore           storage/sqlite_store.py:305
  LLMClient             llm/client.py:25
  shell/fault scripts   faults/*.sh, cluster/*.sh
  kubectl/etcdctl/ssh   tools/k8s.py, node_etcd.py, repairs.py, ssh.py
  Reporter / LiveView   harness/reporter.py:21, harness/ui.py:16
            |
            v
OUTPUTS
  SQLite = source of truth
  runs.jsonl / traces/*.jsonl / logs/*.log = derived
  replay.html / score.py summaries = derived views
```

The honest description: the repository is mid-migration from procedural scripts
to OO. The central loop now has typed boundaries and ports, while runner helpers,
some CLI code, and tool handlers still accept raw dicts for compatibility.

## 2. Main ownership map

| Concern | Current owner | Comment |
| --- | --- | --- |
| Scenario definition + validation | `tools/scenario.py` | Authoritative in runner, loop, and gate policy; dicts coerced at compatibility edges. |
| Cluster identity / topology | `tools/topology.py:516` | Discovers, freezes, hashes, resolves targets; still returns raw dicts. |
| Destructive safety | `tools/safety.py:29` | Enforces context/cluster/topology allow-lists. |
| Backend config | `llm/client.py:17` | `Backend` is typed, but engine receives it untyped. |
| Chat messages | `harness/message.py:10` | `Message` owns shape, redaction, truncation, and persistence. |
| Event/artifact channel | `harness/journal.py` | Owns event mutation, UI descriptions, and artifact serialization. |
| Prompt policy | `harness/prompt.py:9` | `SystemPrompt` renders topology-aware prompt. |
| Tool catalog | `tools/tool.py:235` | Singleton `TOOL_REGISTRY`; dynamic schemas; late-bound repair handlers. |
| Diagnosis/repair gates | `harness/tooling.py:104`, `harness/tooling.py:173` | Real domain policy; good seam. |
| Agent wiring | `harness/agent.py` | `Agent()` has default factories; `build_agent()` is a compatibility shim; `Agent.run` delegates to `AgentLoop`. |
| Response audit | `llm/response.py` | `LLMOutcome` maps success/failure to `Result`; `commit()` records request, artifact, message, and event. |
| Tool lifecycle | `harness/call.py` | `Call` persists intent; `CallExecution` maps `Result` success/failure and commits the result. |
| Checkpointing | `harness/checkpoint.py` | Owns state snapshot, lease renewal, and run-row projection. |
| Verification | `harness/verification.py` | Owns verify execution, `Result` variant, evidence artifact, and event. |
| Orchestration loop | `harness/loop.py` | Coordinates objects and stop conditions; no longer owns per-call persistence details. |
| Persistence | `storage/sqlite_store.py` | Authoritative, event-log-first; sole writer of derived traces/exports. |
| Scoring | `common/scoring.py` | One `Score` used by engine, `score.py`, and `replay.py`. |

### Layering assessment

```text
root CLIs / scripts
  -> harness/agent.py
  -> harness/spec.py -> harness/loop.py
  -> harness/tooling.py, harness/prompt.py, harness/message.py
  -> llm/client.py, tools/tool.py, tools/topology.py, storage/sqlite_store.py
  -> common.*, tools/{cluster_config,ssh,k8s,node_etcd,repairs,truncate}
```

Observed problems with this direction:

- domain/application code calls concrete infrastructure directly;
- `tools/` contains both domain policy and infrastructure adapters;
- infrastructure imports back into `tools`/`common`;
- import cycles are worked around with local imports and cache mutation;
- multiple shims and docs still describe the pre-OO layout.

## 3. Workflow from start to completion

```text
./chaos run --scenario etcd-member-down --backend ollama --runs 1
        |
        v
chaos_cli.main()
  build launch_id, argv, env
  subprocess.call / Popen run_benchmark.py
        |
        v
run_benchmark.main()
  load_env()
  parse args/paths
  load_scenario_object()
  validate scenario
  keep Scenario as the authoritative object
        |
        v
topology + safety
  discover_topology()
  freeze_topology()
  require_destructive_guard()
  capability_check()
  resolve_targets()
        |
        v
epoch/config/run creation
  active_topology_hash check
  stale state discard OR resume refusal
  build_backend()
  build run_env
  compute config_hash
  create_batch()
  create_run()
        |
        v
fresh setup
  lease
  reset_cluster
  baseline verify
  inject fault
  wait_for_fault
        |
        v
harness.spec.ExperimentSpec + RunContext
  scenario, backend, commands, frozen env
  run identity, store, reporter, resume flag
         |
         v
harness.agent.Agent.run(spec, ctx)
  Agent() defaults
    ChatClient adapter (LLMClient)
    TOOL_REGISTRY
    Diagnosis
    Repair
         |
         v
harness.loop.AgentLoop.run(spec, ctx) -> RunResult
  restore/create AgentState
  restore/create Message[]
  Budget / Call / LLMResponse
  SystemPrompt(topology).render()
  Checkpoint.capture()

        |
        v
LLM LOOP
  Message[] -> dicts -> LLMOutcome.invoke(ChatClient.chat())
  Result.success() / Result.failure()
  LLMOutcome.commit() -> LLMResponse | None
    success: add_llm_request + artifact + message + assistant event
    failure: add_llm_request(error) + artifact + llm_error event
  Budget.add_tokens()
  Checkpoint.capture()
        |
        +-- no tool calls + no diagnosis -> Message.user().persist()
        +-- no tool calls + diagnosis -> stop
        +-- tool calls:
              Call.from_tool_call().for_tool(registry.get(name))
              Call.invoke() -> CallExecution
                begin_tool_call intent
                Tool.run(args, ToolContext)
                  diagnosis -> Diagnosis.execute()
                  repair    -> Repair.execute() -> Tool.invoke_handler()
                Result.success() / Result.failure()
              ToolOutcome -> AgentState.apply()
              CallExecution.commit()
                artifact + finish_tool_call + tool message + event
              Checkpoint.capture()
        |
        v
budget/stop condition
  Budget.exhausted_reason() / submitted / provider_error_* / agent_stopped
         |
         v
VERIFY + FINISH
  Verification.run(spec) -> Verification
  Verification.commit()
    artifact + event
  record = {...}
  score = Score.from_record(...)
  store.finish_run()
  store.export_trace()
  store.export_all()

        |
        v
derived views
  score.py --db ...
  ./chaos replay
  chaos status / attach
```

### Detailed handoffs

1. **CLI launch**
   - `./chaos` calls `chaos_cli.main()` at `chaos:1-7`.
   - `run` parser at `chaos_cli.py:238-250`.
   - Launcher builds `launch_id`, argv, and env at `chaos_cli.py:278-326`.
   - Foreground uses `subprocess.call`; detached uses `Popen` with a launch log.

2. **Environment and config**
   - `run_benchmark.py:13-15` calls `load_env()`.
   - `llm/env.py:9-21` loads `.env` with process env taking precedence.
   - `llm/backends.py:11-42` builds provider-specific `Backend` values.
   - Important env names: `CHECKPOINT_DB`, provider keys, `K8S_*`, `SSH_*`,
     `CHAOS_CLUSTER_ID`, `CHAOS_ALLOWED_*`, `CHAOS_TOPOLOGY_JSON`,
     `CHAOS_FAULT_CHECK_MODE`.

3. **Scenario load**
   - `tools/scenario_schema.py:36-42` calls `Scenario.from_file()` and validates.
   - `tools/scenario.py:170-278` checks fields, root cause, repair policy,
     budgets, selectors, and script existence.
   - Runner converts the object back to a dict at `run_benchmark.py:396`.

4. **Topology and safety**
   - `tools/topology.py:516-526` discovers topology from fixture, live cluster,
     or fallback.
   - `tools/safety.py:29-64` enforces destructive allow-lists.
   - `tools/topology.py:637-719` performs capability check.
   - `tools/topology.py:592-634` resolves targets.

5. **Epoch gate and run creation**
   - `storage/sqlite_store.py:131-142` finds active topology hash.
   - Stale fresh runs discard state at `run_benchmark.py:424-435`.
   - Stale resumes refuse at `run_benchmark.py:416-423`.
   - `run_benchmark.py:455-460` builds `run_env`.
   - `run_benchmark.py:462-474` builds `config_hash` input.
   - `run_benchmark.py:523-533` creates a batch.
   - `run_benchmark.py:588-618` creates each run.

6. **Fresh setup**
   - Lease at `run_benchmark.py:128-132`.
   - Reset at `run_benchmark.py:138-144`.
   - Baseline at `run_benchmark.py:146-152`.
   - Injection at `run_benchmark.py:163-176`.
   - Fault check at `run_benchmark.py:178-201`.
   - Reset/baseline/inject/fault failures create terminal records and abort.

7. **Agent loop**
   - `harness/agent.py:32` builds the concrete agent.
   - `harness/engine.py:84-106` restores or initializes state and messages.
   - `harness/engine.py:151-162` calls the LLM with tool schemas.
   - `harness/engine.py:173-199` converts the response into `Message`.
   - `harness/engine.py:201-261` dispatches tool calls.
   - `harness/engine.py:263-270` applies deadline nudges.
   - `harness/engine.py:272-275` sets budget end reasons.

8. **Finish**
   - Final verify at `harness/engine.py:277-288`.
   - Record at `harness/engine.py:301-324`.
   - Score at `harness/engine.py:327-349`.
   - `store.finish_run()` at `storage/sqlite_store.py:1251-1310`.
   - `export_all()` at `storage/sqlite_store.py:1355-1405`.

## 4. Resume path

```text
--resume / --resume-of
        |
        v
select candidate run: status in {running, interrupted}
        |
        v
refuse if:
  topology mismatch
  scenario/backend mismatch
  unknown mutating tool call
  unsafe phase
  config_hash mismatch without --force-resume
        |
        +-- unsafe -> abort old run -> create "<old>-retryN" -> fresh setup
        |
        +-- safe -> mark unfinished read-only calls superseded
                   engine(resume=True)
                   restore state + messages
                   continue LLM loop
                   verify + finish
```

Resume details:

- CLI flags at `chaos_cli.py:248-249` and `run_benchmark.py:365-367`.
- Candidate selection at `run_benchmark.py:535-552`.
- Retry decision at `run_benchmark.py:556-568`.
- Retry creation at `run_benchmark.py:314-352`.
- In-place resume at `run_benchmark.py:243-311`.
- Engine restore at `harness/engine.py:84-106`.
- State authority/cache rules at `storage/sqlite_store.py:1098-1183`.
- `interrupted` is recognized as a resume status but is not written by the
  runner; crashed runs usually remain `running`.

## 5. Design gaps

### P0 — correctness and safety

1. **Final verification env asymmetry**
   - Baseline and injection get frozen topology env via `run_env` at
     `run_benchmark.py:148` and `run_benchmark.py:164`.
   - Final verify uses inherited env only at `harness/engine.py:281`.
   - `CHAOS_TOPOLOGY_JSON`, `TARGET_NODE`, and `TARGET_NODES` may be absent
     during final verification.

2. **Fixture fault-check env is child-only**
   - `CHAOS_FAULT_CHECK_MODE` is set in `run_env` at
     `run_benchmark.py:455-460`.
   - The predicate reads the runner process env at
     `tools/fault_checks.py:165-168`.
   - In-fixture in-process checks can disagree with shell checks.

3. **Gate receipt lost on state rebuild**
   - Approval event omits the receipt at `harness/tooling.py:149-157`.
   - Rebuild expects it at `storage/sqlite_store.py:1123-1128`.
   - Cached `state_json` hides this until it is invalidated.
   - The receipt is a correctness handshake, not a secret; see §8.1.

4. **Lease can expire during legitimate work**
   - 300s lease at `storage/sqlite_store.py:1063-1077`.
   - Repair readiness waits up to 180s at `tools/repairs.py:19-35`.
   - LLM retries can also be long at `llm/client.py:76-90`.
   - No renewal exists.

5. **Redaction asymmetry**
   - Traces redact gate receipts in prompt mode at `harness/engine.py:198`.
   - SQLite `messages` rows and `llm_response_full` store raw tool calls and
     responses.
   - Redaction must move to write time; see §8.1.

6. **Shell interpolation risk**
   - `shell=True` at `run_benchmark.py:53` and `harness/engine.py:281`.
   - `TARGET_NODE` / `TARGET_NODES` are interpolated without `shlex.quote` at
     `run_benchmark.py:459-460`.
   - Scenario validation checks only script existence at
     `tools/scenario.py:267-276`.

7. **Prompt-mode gate-receipt contradiction**
   - Schema marks `approval_token` required for every repair at
     `tools/tool.py:217-231`.
   - Gate message says the receipt is suppressed at
     `harness/tooling.py:145-146`.
   - The model is implicitly asked to guess `approved:<run_id>`.
    - Resolution: after operator confirmation, return the receipt to the model;
      on decline, withhold it and block repair. The harness state is authoritative,
      so a redacted or absent wire value is accepted on resume. `interactive=1`
      marks the run; see §8.1.

8. **Unsafe/unknown mutating tool call handling is resume-critical**
   - `has_unknown_mutating_tool_call()` at
     `storage/sqlite_store.py:943-952` forces abort-and-retry.
   - Correctness currently depends on tool names and call status rows rather
     than a typed command/result model.

### P1 — architecture and maintainability

1. **`Scenario` is not authoritative**
   - Object validates at `tools/scenario.py:56-67`.
   - Runner converts to dict at `run_benchmark.py:396`.
   - Repair policy is re-read from raw dict at `harness/tooling.py:201-204`.
   - `Scenario.allows_repair()` exists at `tools/scenario.py:138` but is unused.

2. **Two `run_scenario` surfaces**
   - Facade at `harness/agent.py:42`.
   - Engine implementation at `harness/engine.py:19`.
   - Both carry long overlapping signatures.
   - `Agent.run(*args, **kwargs)` has no explicit contract.
   - Passing injected keys through the facade causes duplicate-keyword errors.

3. **No storage port**
   - Engine takes `store: Any` and directly calls `add_event`,
     `append_message`, `finish_run`, etc.
   - SQLite is both event log and state authority, but the dependency is
     implicit.

4. **Three definitions of success**
   - Engine score: `harness/engine.py:327-349`.
   - Scorer rules: `score.py:25-116`.
   - Replay matrix: `replay.py:44-82`.
   - JSONL and DB filters differ at `score.py:52-64` and
     `storage/sqlite_store.py:1320-1333`.

5. **State exists four times**
   - `AgentState` object.
   - `runs.state_json` cache.
   - Append-only `state_snapshots`.
   - Scalar `runs.*` columns.
   - `diagnosis_correct` is computed at both `harness/tooling.py:92` and
     `harness/engine.py:125`.

6. **Trace written twice**
   - Engine appends directly to `traces/<run_id>.jsonl`.
   - `export_all()` rewrites the same files from SQLite at
     `storage/sqlite_store.py:1375-1383`.
   - Crash windows can diverge even though SQLite claims authority.

7. **Tool dispatch has too many layers**
   - `Tool.run()` at `tools/tool.py:100`.
   - `Diagnosis.execute()` / `Repair.execute()` at `harness/tooling.py:119`
     and `harness/tooling.py:188`.
   - `Tool.invoke_handler()` at `tools/tool.py:95`.
   - Repair success is inferred from string prefixes at
     `harness/tooling.py:226-230`.

8. **`tools/` mixes domain and infrastructure**
   - Domain: `scenario.py`, `topology.py`, `safety.py`, `fault_checks.py`.
   - Infrastructure: `ssh.py`, `k8s.py`, `node_etcd.py`,
     `cluster_config.py`, `repairs.py`.
   - This is why globals and patch points leak into policy.

9. **Primitive-obsession leftovers**
   - Capability dict at `tools/topology.py:709-719`.
   - `config_hash` input at `run_benchmark.py:462-473`.
   - Fault payload at `run_benchmark.py:181-187`.
   - Record at `harness/engine.py:301-324`.
   - Score at `harness/engine.py:337-349`.
   - State snapshot at `harness/tooling.py:69-85`.
   - `Backend` is typed at `llm/client.py:17` but untyped in engine.
   - `call_id` is derived from counters at `harness/engine.py:228`.

10. **Destructive policy is scattered**
    - Guard: `tools/safety.py:29-64`.
    - Capability: `tools/topology.py:637-719`.
    - Min targets: `run_benchmark.py:409-411`.
    - Gate order: `harness/tooling.py:188-216`.
    - Prompt rules: `harness/prompt.py:43-61`.
    - Cluster nuke confirmation: `chaos_cli.py:348-423`.

11. **`mode` changes only topology eligibility**
    - `tools/topology.py:643-684`.
    - Repair mode still resets and injects; documented caveat at
      `README.md:317-321`.

12. **`forbidden_reason` depends on substring `"forbidden"`**
    - `harness/tooling.py:205-216`.
    - `"repair not acceptable"` becomes a policy violation but not a forbidden
      repair.

13. **Dependency injection is brittle**
    - Tests patch `agent.LLMClient` at `tests/test_gate.py:58-59` and
      `tests/test_resume.py:122`.
    - Engine imports its own `LLMClient` at `harness/engine.py:13`.
    - This works because `Agent()` default factories read the patched module
      global at construction time; `build_agent()` keeps the old call site.

14. **Whole-flow testability is weak**
    - `run_benchmark.main()` and `chaos_cli.main()` mix `sys.argv`,
      `sys.exit`, subprocesses, and globals.
    - Tests patch env and fixture topology instead of passing config objects.
    - Prompt approval requires patching `builtins.input` at
      `harness/tooling.py:130`.

15. **Fixture mode bypasses live checks but still requires destructive guard**
    - Fixture fault check at `tools/fault_checks.py:165-168`.
    - Guard always runs at `run_benchmark.py:398-414`.
    - Offline tests must fabricate cluster identity allow-lists.

16. **Node-role vocabulary is duplicated**
    - `tools/scenario.py:16-19` has 10 ordinal roles.
    - `tools/topology.py:17-22` has 20 ordinal roles.
    - Both are aliased through `tools/cluster_config.py:21`.

### P2 — observability, delivery, and coherence

1. **Streaming failures are invisible**
   - `llm/client.py:169-173` silently falls back from streaming to
     non-streaming.
   - No event or `llm_requests` row records the failed streaming attempt.
   - Reconstructed responses hardcode `finish_reason="stop"` at
     `llm/client.py:147-150`.

2. **`verify_output` lost on export**
   - Present in the final record at `harness/engine.py:319`.
   - Missing from `RECORD_TO_COLUMN` at `storage/sqlite_store.py:90-111`.
   - Therefore absent from promoted `runs` columns and reconstructed exports.

3. **Derived files rewritten after every run**
   - `export_all()` at `run_benchmark.py:238-240` and
     `run_benchmark.py:613-617`.
   - Multi-run sweeps are serial at `run_benchmark.py:588`.
   - Concurrent runs can clobber derived files even with per-run leases.

4. **`./chaos run` cannot set `--batch-id`**
   - Present only on `run_benchmark.py:364`.
   - Absent from CLI command construction at `chaos_cli.py:284-304`.
   - `launch_id` doubles as `batch_id` at `run_benchmark.py:506` and
     `run_benchmark.py:608`.

5. **Score-version precedence is resolved**
   - `scores` remains keyed by `(run_id, scorer_version)`.
   - New runs write `strict-loop-v2` from `common/scoring.py`.
   - `SQLiteStore.load_records()` prefers the current scorer, then newest
     `created_at`, before merging score fields into records.
   - Legacy score rows remain readable as fallback values.

6. **Nudge state nondeterminism**
   - Snapshot contains `diagnosis_nudged` at `harness/tooling.py:82`.
   - Rebuild derives it from `nudge` events at
     `storage/sqlite_store.py:1129-1130`.
   - The early no-tool nudge appends a message but does not log a `nudge`
     event at `harness/engine.py:202-209`.
   - Resume can re-nudge.

7. **Env loading is inconsistent**
   - `chaos_cli.py` does not call `load_env()`.
   - `.env` loading happens only in `run_benchmark.py:14-15`.
   - `score.py`, `replay.py`, and `db_cli.py` rely on process env.
   - DB defaults are computed independently at `chaos_cli.py:203`,
     `run_benchmark.py:363`, `db_cli.py:22`, and `replay.py:213`.

8. **Parallel public surfaces over the same singletons**
   - `harness/__init__.py:1-3`.
   - `tools/schemas.py:1-31`.
   - `harness/agent.py` re-exports patch surfaces.
   - `tools/tool.py` exposes singleton `TOOL_REGISTRY`.

9. **Stale or dead abstractions**
   - `common/xmlform.py:1-149` is self-contained but unused by the run flow.
   - `tools/schemas.py` is a shim while the engine calls `registry.schemas()`
     directly.
   - `DESIGN.md:238` still says schemas are generated by `tools/schemas.py`.

10. **Docs drift — resolved in documentation**
    - README now says injection failure aborts at `run_benchmark.py:170-176`.
    - README now scopes “read-only” to diagnostic tools; repairs are gated and
      separate.

## 6. Draft target architecture

```text
domain/
  Scenario                owns budgets, repair policy, targets
  TopologyPolicy          owns hash, capability, target selection
  ExperimentSpec          scenario + backend + guard + run_env + config_hash
  Message                 chat shape + redaction + truncation + persistence
  LLMOutcome              LLM Result variant + audit trail commit
  LLMResponse             response parsing + success audit trail commit
  Call                    tool call identity + intent persistence
  CallExecution           Result variant + post-execution persistence
  Journal                 event/artifact/UI channel
  Verification            verify run + evidence persistence
  ToolOutcome             typed result, no string-prefix success inference
  AgentState              pure state machine
  Diagnosis / Repair      gates
  Score                   single definition of success

application/
  RunExperiment           reset/baseline/inject/fault-check/verify
  AgentLoop               one typed run method, no 20-arg facade duplication
  RunResult               record + score + terminal reason
  ports:
    RunStore
    ChatClient
    CommandRunner
    Reporter

adapters/
  SQLiteStore
  LLMClient / Backend
  ShellCommandRunner
  Kubectl/Etcd/SSH tools
  UiReporter / LiveView / Replay

composition/
  run_benchmark.main()
  Agent() defaults / build_agent() shim
  chaos_cli.main()
```

## 7. Refactor sequence

Safety fixes went first, then the typed boundary objects, then the loop seam.
The legacy dict facade remains only as a compatibility adapter.

Completed in the current hardening pass:

1. Verify env symmetry, fixture fault-check env, fixture opt-in, evidence
   preservation on topology change, shell/argument validation, receipt
   rebuild/redaction, diagnosis attempt cap, and lease renewal.
2. One `Score` object used by engine, `score.py`, and `replay.py`; explicit
   scorer-version precedence; `verify_output` in records.
3. Streaming fallback visibility and redacted derived traces/exports.
4. `Scenario` is authoritative in runner, engine, and gate policy; dicts are
   coerced only at compatibility boundaries and serialization.
5. `ExperimentSpec` / `RunContext`, `Budget`, `Call`, `LLMResponse`, `Result`,
   `Approval`, and `Refusal` carry the previously untyped boundary state.
6. One typed `AgentLoop.run(spec, ctx) -> RunResult`; `run_scenario` is a legacy
   adapter, and `Agent.run` is composition rather than the loop.
7. `Message`, `LLMOutcome`/`LLMResponse`, `Call`/`CallExecution`, `Journal`, and
   `Verification` own their persistence details; the loop only coordinates.
8. `RunStore` and `ChatClient` ports define the loop dependencies; the SQLite
   store is the only writer of derived traces and exports.

Remaining:

9. Consolidate policy into `TopologyPolicy` and state into events plus a
   rebuildable projection.
10. Move code incrementally into the target layout in §6 with `tools/` re-export
    shims.

## 8. Review-driven design decisions

These decisions resolve the open design calls in `reviews.md`. They are recorded
here rather than in a new document to keep the doc surface small.

### 8.1 Gate receipt, not secret

The value named `approval_token` in the wire format is a gate receipt: a
protocol marker proving that diagnosis was accepted before repair. It is not a
secret and not human approval. Safety comes from canonical forbidden/unacceptable
repair policy and recorded `policy_violations`. The implementation gates on
harness-side state, records enough event data for rebuild, and redacts receipt-like
values from messages, traces, and artifacts at write time. Prompt mode adds an
operator confirmation: it marks the run `interactive=1`, returns the receipt only
when the operator approves, and withholds it on decline. Because the harness state
is authoritative, a redacted or absent wire value is accepted after resume instead
of asking the model to guess a suppressed value.

### 8.2 Aliases are display-only

`kube_etcd_switch_rebuild` canonicalizes to `member_remove_readd`.
`reset_cluster` and `snapshot_restore_full` canonicalize to
`full_cluster_reset`. Aliases inherit the target's repair policy and scoring
outcome; a schema invariant must reject alias/target policy divergence.

### 8.3 Fixture mode is test-only

`CHAOS_TOPOLOGY_JSON` remains available for tests and offline inspection. Normal
`./chaos run` must refuse fixture topology unless an explicit test-only opt-in
is set. The destructive identity and topology guard still applies.

### 8.4 Evidence is preserved on topology change

A topology hash mismatch archives the active epoch to `archive/<timestamp>/`
and exits. `--discard-stale` is required for intentional deletion. Resume always
refuses a mismatch.

### 8.5 Headline metric

`strict_loop_success` is the only headline metric. `loop_success` is retained
only for historical comparison. One `Score` object owns the definition and is
used by the engine, `score.py`, and `replay.py`.

### 8.6 Incremental OO migration

The target layout in §6 is a destination, not a big-bang rewrite. `Score`,
`ExperimentSpec`, `RunContext`, `RunStore`, and `ChatClient` have landed; the
legacy `run_scenario` facade and `tools/` re-exports keep imports stable while
remaining policy and infrastructure modules move incrementally.
