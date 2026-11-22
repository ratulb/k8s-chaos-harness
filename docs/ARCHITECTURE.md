# Architecture draft (reverse-engineered)

Status: audited against the implementation at HEAD `5df4539`. This document is a
reverse-engineered architecture draft plus a hardening-status ledger, not a frozen
snapshot. The original review baseline was `1952bb2`, retained only as provenance.
Line references are relative to the repository root and are refreshed where they
matter; current code wins.

This document captures:

- the architecture that exists today;
- the object/model boundaries introduced so far;
- how a scenario flows from operator start to final output;
- the resume path;
- historical design gaps with their current status;
- a candidate target architecture and refactor sequence.

## 1. Current architecture, as built

```text
OPERATOR / ENTRY POINTS
  ./chaos ping ...          chaos_cli.py:214   no-cluster LLM smoke test
  ./chaos run ...           chaos_cli.py:250
  ./chaos abort|state|replay|status|attach
  ./chaos cluster|target|scenario|probe|verify|nuke
  menu.sh / run_benchmark.sh
  db_cli.py / score.py / replay.py
            |
            v
COMPOSITION ROOT
  run_benchmark.py:385  main()
    - args + .env
    - Scenario + ValidationReport
    - Topology / safety guard / capability / targets / fixture opt-in
    - Backend
    - SQLiteStore
    - run_env + config_hash
    - batch/run rows
            |
            v
EXPERIMENT SETUP / RESUME
  run_benchmark.py:124  _run_fresh()
    lease -> reset -> baseline -> inject -> wait_for_fault
  run_benchmark.py:265  _run_resume()
    restore lease/state/messages; no reset/inject
  run_benchmark.py:344  _abort_and_retry()
    archive evidence -> create "<old>-retryN" -> fresh setup
            |
            v
AGENT COMPOSITION
  harness/agent.py  Agent() defaults / build_agent() shim
    defaults: ChatClient adapter + TOOL_REGISTRY + Diagnosis + Repair
    Agent.run(spec, ctx) delegates to AgentLoop
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
  harness/engine.py is now a thin legacy adapter, not the main loop
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
  SQLiteStore           storage/sqlite_store.py:390
  LLMClient             llm/client.py
  shell/fault scripts   faults/*.sh, cluster/*.sh
  kubectl/etcdctl/ssh   tools/k8s.py, node_etcd.py, repairs.py, ssh.py
  Reporter / LiveView   harness/reporter.py, harness/ui.py
            |
            v
OUTPUTS
  SQLite = source of truth
  runs.jsonl / traces/*.jsonl = SQLite-derived exports
  logs/*.log = direct runner debug logs
  replay.html / score.py summaries = derived views
```

The honest description: the repository is mid-migration from procedural scripts
to OO. The central loop now has typed boundaries and ports, while runner helpers,
some CLI code, and tool handlers still accept raw dicts for compatibility.

## 2. Main ownership map

| Concern | Current owner | Comment |
| --- | --- | --- |
| Scenario definition + validation | `tools/scenario.py`, `tools/scenario_schema.py` | Typed `Scenario` is authoritative in runner, spec, loop, and gate policy; dicts coerced only at compatibility/serialization edges. |
| Cluster identity / topology | `tools/topology.py` | `discover_topology()`, `freeze_topology()`, `resolve_targets()`, and `capability_check()` still return raw dicts. |
| Destructive safety | `tools/safety.py` | `require_destructive_guard()` enforces context/cluster/topology allow-lists. |
| Backend config | `llm/client.py`, `llm/backends.py` | `Backend` is typed; `ExperimentSpec` still carries it as `Any`. |
| Chat messages | `harness/message.py` | `Message` owns shape, redaction, truncation, and persistence. |
| Event/artifact channel | `harness/journal.py` | Owns event mutation, redacted UI descriptions, and artifact serialization; raw event payloads remain in SQLite when callers pass them. |
| Prompt policy | `harness/prompt.py` | `SystemPrompt` renders topology-aware prompt. |
| Tool catalog | `tools/tool.py` | Singleton `TOOL_REGISTRY`; dynamic schemas; late-bound repair handlers. |
| Diagnosis/repair gates | `harness/tooling.py` | `Diagnosis.execute()` and `Repair.authorize()` are the real domain policy seam. |
| Agent wiring | `harness/agent.py` | `Agent()` has default factories; `build_agent()` and `run_scenario()` are compatibility shims; `Agent.run` delegates to `AgentLoop`. |
| Response audit | `llm/response.py` | `LLMOutcome` maps success/failure to `Result`; `commit()` records request, artifact, message, and event. |
| Tool lifecycle | `harness/call.py` | `Call` persists intent; `CallExecution` maps `Result` success/failure and commits the result. |
| Checkpointing | `harness/checkpoint.py` | Owns state snapshot, lease renewal, and run-row projection. |
| Verification | `harness/verification.py` | Owns verify execution, `Result` variant, evidence artifact, and event. |
| Orchestration loop | `harness/loop.py` | Coordinates objects and stop conditions; no longer owns per-call persistence details. |
| Persistence | `storage/sqlite_store.py` | Authoritative, event-log-first; sole writer of derived traces/exports. |
| Scoring | `common/scoring.py` | One `Score` used by loop, `score.py`, and `replay.py`. |

### Layering assessment

```text
root CLIs / scripts
  -> run_benchmark.py main() / chaos_cli.py main()
  -> harness/agent.py Agent.run(spec, ctx)
  -> harness/loop.py AgentLoop.run(spec, ctx)
  -> harness/tooling.py, harness/prompt.py, harness/message.py, harness/verification.py
  -> llm/client.py, tools/tool.py, tools/topology.py, storage/sqlite_store.py
  -> common.*, tools/{cluster_config,ssh,k8s,node_etcd,repairs,truncate}
```

`ExperimentSpec` and `RunContext` are typed data inputs to this chain, not a
routing layer.

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
  reject fixture topology unless CHAOS_ALLOW_FIXTURE_RUNS=1
  require_destructive_guard()
  capability_check()
  resolve_targets()
        |
        v
epoch/config/run creation
  active_topology_hash check
  stale state: archive + exit by default; --discard-stale deletes
  stale resume: refuse
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
  Verification.run(spec, env=spec.run_env) -> Verification
  Verification.commit()
    artifact + event
  record = {...}
  score = Score.from_record(...)
  store.finish_run()
  store.export_trace()
  runner then calls store.export_all() after agent return or setup abort

        |
        v
derived views
  score.py --db ...
  ./chaos replay
  chaos status / attach
```

### Detailed handoffs

0. **No-cluster LLM smoke test**
   - `./chaos ping ...` parser at `chaos_cli.py:257-259`; dispatch at
     `chaos_cli.py:332-333`.
   - `_ping()` at `chaos_cli.py:214-247` calls `build_backend()`, checks
     `LLMClient.health()`, and then runs one `LLMOutcome.invoke()` completion.
   - It does not load scenarios or touch the cluster; missing endpoint/key
     returns setup guidance.

1. **CLI launch**
   - `./chaos` calls `chaos_cli.main()` at `chaos:8-10`.
   - `run` parser at `chaos_cli.py:291-304`; flags include `--resume-of`,
     `--force-resume`, and `--discard-stale`.
   - Launcher builds `launch_id`, argv, and env at `chaos_cli.py:341-355`.
   - Foreground uses `subprocess.call`; detached uses `Popen` with a launch log.

2. **Environment and config**
   - Both `chaos_cli.py:252` and `run_benchmark.py:16` call `load_env()`.
   - `llm/env.py` uses `os.environ.setdefault`, so process env wins.
   - Env names include `CHECKPOINT_DB`, `CHAOS_PING_BACKEND`,
     `CHAOS_LOCAL_NODE`, `CHAOS_TOPOLOGY_HASH`, `CHAOS_TOPOLOGY_JSON`,
     `CHAOS_ALLOW_FIXTURE_RUNS`, `CHAOS_CLUSTER_ID`, `CHAOS_ALLOWED_*`,
     `CHAOS_FAULT_CHECK_MODE`, provider keys, `K8S_*`, and `SSH_*`.

3. **Scenario load**
   - `tools/scenario_schema.py:36-42` calls `Scenario.from_file()` and validates.
   - `tools/scenario.py` checks fields, root cause, repair policy, budgets,
     selectors, and script existence.
   - Runner keeps the `Scenario` object at `run_benchmark.py:420-427`; dict
     coercion remains only for hashing/registration at `run_benchmark.py:492`.

4. **Topology and safety**
   - `tools/topology.py:518-539` discovers and freezes topology.
   - Fixture topology is rejected unless `CHAOS_ALLOW_FIXTURE_RUNS=1` at
     `run_benchmark.py:448-453`.
   - `tools/safety.py:29-64` enforces destructive allow-lists.
   - `tools/topology.py:639-720` performs capability checks.
   - `tools/topology.py:594-636` resolves targets.

5. **Epoch gate and run creation**
   - `SQLiteStore.active_topology_hash()` is at
     `storage/sqlite_store.py:157-168`.
   - Fresh topology mismatch archives the epoch and exits at
     `run_benchmark.py:455-489`; deletion requires `--discard-stale`.
   - Resume mismatch refuses at `run_benchmark.py:600-607`.
   - `run_benchmark.py:510-515` builds `run_env`.
   - `config_hash` input is built near `run_benchmark.py:517-529`.
   - `create_batch()` and `create_run()` remain in `run_benchmark.main()`.

6. **Fresh setup**
   - Lease at `run_benchmark.py:144-150`.
   - Reset at `run_benchmark.py:152-158`.
   - Baseline at `run_benchmark.py:160-166`.
   - Injection at `run_benchmark.py:168-180`.
   - Fault check at `run_benchmark.py:182-206`.
   - Reset/baseline/inject/fault failures create terminal records and abort.

7. **Agent loop**
   - `Agent.run()` at `harness/agent.py:24-31` delegates to `AgentLoop`.
   - `harness/loop.py:57-59` restores or initializes state and messages.
   - `harness/loop.py:136-170` calls the LLM, persists responses, applies
     tool outcomes, and emits nudges.
   - `harness/loop.py:172-185` sets budget end reasons and verification phase.
   - Legacy `engine.run_scenario()` remains only a compatibility adapter at
     `harness/engine.py:37-108`.

8. **Finish**
   - Final verify at `harness/verification.py:17-44` with `spec.run_env`.
   - Record and score are created in `harness/loop.py:193-211`.
   - `SQLiteStore.finish_run()` is at `storage/sqlite_store.py:1251-1310`.
   - `SQLiteStore.export_trace()` and `export_all()` are at
     `storage/sqlite_store.py:1515-1575`; the runner owns the final
     `export_all()`.

## 4. Resume path

```text
--resume / --resume-of
        |
        v
select candidate run: status in {running, interrupted}
        |
        v
triage:
  topology mismatch        -> refuse
  unsafe phase             -> abort + retry
  config_hash mismatch     -> abort + retry unless --force-resume
  unknown mutating call    -> abort + retry
  scenario/backend mismatch-> skip candidate and continue
        |
        +-- unsafe -> abort old run -> create "<old>-retryN" -> fresh setup
        |
        +-- safe -> mark unfinished read-only calls superseded
                   Agent().run(spec, ctx with resume=True)
                   AgentLoop restores state + messages
                   continue LLM loop
                   verify + finish
```

Resume details:

- CLI flags at `chaos_cli.py:301-304`; runner flags at `run_benchmark.py:395-399`.
- Candidate selection at `run_benchmark.py:590-597`.
- Retry decision at `run_benchmark.py:599-623`.
- Retry creation at `run_benchmark.py:344-382`.
- In-place resume at `run_benchmark.py:265-341`.
- Loop restoration at `harness/loop.py:57-69`; engine is not on this path.
- State authority/cache rules at `storage/sqlite_store.py:1218-1318`.
- `interrupted` is recognized as a resume status but is not written by the
  runner; crashed runs usually remain `running`.

## 5. Historical design gaps with current status

Status legend: **fixed**, **partial**, **open**, or **resolved in docs**. These
were found by tracing the baseline implementation; the status text is current at
HEAD `5df4539`.

### P0 — correctness and safety

1. **Final verification env asymmetry — fixed**
   - `ExperimentSpec` carries frozen `run_env`, populated on both fresh and
     resume paths at `run_benchmark.py:229-239` and `run_benchmark.py:313-323`.
   - Final verify receives `spec.run_env` at `harness/verification.py:20-27`
     and `harness/loop.py:175-180`.

2. **Fixture fault-check env is child-only — fixed**
   - `wait_for_fault(..., env=run_env)` is called at `run_benchmark.py:512-513`.
   - `check_fault_present(..., env)` reads the passed env at
     `tools/fault_checks.py:167-175` and `tools/fault_checks.py:204-222`.

3. **Gate receipt lost on state rebuild — fixed**
   - Approval events persist `approval_token` at `harness/tooling.py:194-205`.
   - Rebuild reads it from the event and refreshes receipt-derived keys even on
     cache hits at `storage/sqlite_store.py:1256` and
     `storage/sqlite_store.py:1281-1289`.
   - The receipt is a correctness handshake, not a secret; see §8.1.

4. **Lease can expire during legitimate work — partial**
   - `Checkpoint.capture()` renews the lease at `harness/checkpoint.py:25-36`
     and is called at loop boundaries in `harness/loop.py:113`, `:139`, `:162`,
     and `:170`.
   - There is no background renewal; a long LLM retry burst or a 180s repair
     wait can still exceed the 300s lease between checkpoints
     (`llm/client.py:76-90`, `tools/repairs.py:19-35`).

5. **Redaction asymmetry — partial**
   - `Message.to_stored_dict()` redacts message payloads at write time, and
     derived traces/exports redact via `Message.redact_payload()`
     (`harness/message.py:102-120`, `storage/sqlite_store.py:1520-1526`).
   - `Journal.event()` still passes the raw event payload to SQLite while using
     a redacted copy only for UI descriptions (`harness/journal.py:18-23`,
     `storage/sqlite_store.py:937-947`).
   - The raw approval receipt in the `approval` event is intentional for rebuild;
     other raw tool args/results in event payloads are not. See §8.1.

6. **Shell interpolation risk — partial**
   - `shell=True` remains in runner/verification paths
     (`run_benchmark.py:65`, `harness/verification.py:20-27`).
   - Target names are now allow-list validated before entering `run_env`
     (`tools/scenario.py:304-313`), closing the practical injection path.
   - Scenario commands still interpolate `$TARGET_NODE` without `shlex.quote`
     (`scenarios/etcd-member-down.json:8`).

7. **Prompt-mode gate-receipt contradiction — fixed**
   - Repair schemas now require `approval_token` (`tools/tool.py:221-238`).
   - Prompt mode returns the receipt only after operator approval, withholds it
     on decline, and accepts redacted/absent wire values against authoritative
     harness state (`harness/tooling.py:146-177`, `harness/tooling.py:256-271`).
   - `interactive=1` marks the run; see §8.1.

8. **Unsafe/unknown mutating tool call handling is resume-critical — partial**
   - Typed `Call` and `CallExecution` now persist intent/results
     (`harness/call.py:13-125`).
   - Resume safety still keys off `tool_calls` rows (`mutating` + status) and
     repair-name sets rather than a fully typed command/result model
     (`storage/sqlite_store.py:967-1030`, `:1054-1063`,
     `run_benchmark.py:616-623`).

### P1 — architecture and maintainability

1. **`Scenario` is not authoritative — partial**
   - Typed `Scenario` is authoritative in runner, spec, loop, and gate policy
     (`run_benchmark.py:420-427`, `:492`, `harness/spec.py:11`,
     `harness/tooling.py:49-56`).
   - Raw dicts remain only at hashing/legacy edges.
   - `Scenario.allows_repair()` is used only by tests, not production.

2. **Two `run_scenario` surfaces — partial**
   - `Agent.run(spec, ctx) -> RunResult` is now an explicit typed contract.
   - `Agent.run_scenario()` and `engine.run_scenario()` remain as compatibility
     facades; duplicate-keyword forwarding is gone
     (`harness/agent.py:24-31`, `harness/engine.py:37-108`).

3. **No storage port — fixed**
   - `RunStore` and `ChatClient` protocols exist in `harness/ports.py:10-104`.
   - `AgentLoop` casts `ctx.store` to `RunStore` at `harness/loop.py:48-55`.
   - Legacy engine still takes `store: Any` but is only an adapter.

4. **Three definitions of success — partial**
   - One `Score` definition is used by loop, `score.py`, and `replay.py`
     (`common/scoring.py:40-113`).
   - JSONL and DB inclusion filters still differ
     (`score.py:35-46` vs `storage/sqlite_store.py:1470-1480`).

5. **State exists four times — open**
   - `AgentState`, `runs.state_json`, `state_snapshots`, and scalar `runs.*`
     columns all remain.
   - `diagnosis_correct` is computed in state apply, loop record, checkpoint
     projection, and event rebuild (`harness/tooling.py:31-99`,
     `harness/loop.py:193`, `harness/checkpoint.py:38-53`,
     `storage/sqlite_store.py:31-83`).

6. **Trace written twice — fixed**
   - The loop no longer writes trace files.
   - `SQLiteStore.export_trace()` and `export_all()` are the only writers
     (`harness/loop.py:214-215`, `storage/sqlite_store.py:1515-1575`).

7. **Tool dispatch has too many layers — open**
   - Chain is now `Call.invoke()` → `Tool.run()` → `Diagnosis`/`Repair.execute()`
     → `Tool.invoke_handler()` (`harness/call.py:48-125`,
     `tools/tool.py:99-125`, `harness/tooling.py:210-329`).
   - Success/failure is still inferred from `ERROR:` and
     `timeout waiting for ...` prefixes (`harness/tooling.py:316`, `:320`).

8. **`tools/` mixes domain and infrastructure — open**
   - Domain policy and SSH/kubectl/etcd adapters still share `tools/`.
   - Application code imports `tools.topology` directly (`harness/loop.py:21`).

9. **Primitive-obsession leftovers — partial**
   - `Score`, `Call`, and `Message` are objects now.
   - Capability dicts, config/fault/record/state dicts, and untyped
     `backend: Any` remain (`tools/topology.py:639-719`,
     `run_benchmark.py:197-203`, `harness/loop.py:182-206`,
     `harness/spec.py:12`).

10. **Destructive policy is scattered — open**
    - Guard, capability checks, minimum targets, gate order, prompt rules, and
      nuke confirmation remain in separate modules with no `TopologyPolicy`.
      (`tools/safety.py:29-64`, `tools/topology.py:639-719`,
      `harness/tooling.py:256-292`, `harness/prompt.py:43-61`,
      `chaos_cli.py:407-415`).

11. **`mode` changes only topology eligibility — open**
    - `mode` affects capability checks and UI header only; fresh setup still
      resets/baselines/injects (`tools/topology.py:639-719`,
      `run_benchmark.py:138-205`, `README.md:358-361`).

12. **`forbidden_reason` depends on substring `"forbidden"` — open**
    - `"repair not acceptable..."` is still classified as policy violation,
      not forbidden repair (`harness/tooling.py:272-275`, `:283`, `:305`).

13. **Dependency injection is brittle — open**
    - Tests still patch `agent.LLMClient`; `Agent`/`AgentLoop` default factories
      read the module global at construction, and engine imports its own client
      (`tests/test_gate.py:58-59`, `harness/agent.py:19`, `harness/loop.py:42`,
      `harness/engine.py:10`).

14. **Whole-flow testability is weak — partial**
    - `chaos_cli.main(argv=None)` is injectable and returns `int`
      (`chaos_cli.py:250`, `:714`).
    - `run_benchmark.main()` still parses `sys.argv` and calls `sys.exit`.
    - Prompt approval still patches `builtins.input`.

15. **Fixture mode bypasses live checks but still requires destructive guard — open**
    - Fixture fault checks short-circuit at `tools/fault_checks.py:173-175`.
    - The guard runs before the fixture opt-in check (`run_benchmark.py:430-432`,
      `:448-453`); offline tests still fabricate allow-lists.

16. **Node-role vocabulary is duplicated — open**
    - `tools/scenario.py:17-20` has 10 ordinal roles.
    - `tools/topology.py:19-24` has 20 ordinal roles.
    - Both are aliased through `tools/cluster_config.py:21`.

### P2 — observability, delivery, and coherence

1. **Streaming failures are invisible — partial**
   - Fallback still silently swallows the exception, but `stream_fallback` and
     hard failures are now recorded in events/artifacts and `llm_requests` rows
     (`llm/client.py:148`, `:170-177`, `llm/response.py:59-75`).
   - The failed streaming attempt has no dedicated event.
   - Reconstructed responses use `finish_reason="reconstructed"`, not `"stop"`.

2. **`verify_output` lost on export — fixed**
   - The column and `RECORD_TO_COLUMN` mapping exist at
     `storage/sqlite_store.py:66` and `:112-137`.
   - Verification commits it into the record (`harness/verification.py:43-44`,
     `harness/loop.py:201`).

3. **Derived files rewritten after every run — open**
   - `export_all()` is still called on terminal/finish paths and rewrites all
     runs/traces (`run_benchmark.py:174`, `:189`, `:216`, `:261`, `:340`,
     `:569`, `:640`, `:643`, `:675`).
   - Multi-run sweeps remain serial.

4. **`./chaos run` cannot set `--batch-id` — partial**
   - CLI still lacks `--batch-id` (`chaos_cli.py:291-304`).
   - Runner honors `args.batch_id` (`run_benchmark.py:394`, `:533-543`,
     `:578-587`).
   - `launch_id` no longer doubles as `batch_id`; it only supplies a fallback.

5. **Score-version precedence is resolved — fixed**
   - `scores` remains keyed by `(run_id, scorer_version)`.
   - `SQLiteStore.load_records()` prefers the current scorer, then newest
     `created_at`, before merging score fields into records
     (`common/scoring.py:8`, `storage/sqlite_store.py:690`, `:1386-1389`,
     `:1492-1498`).

6. **Nudge state nondeterminism — open**
   - Snapshot contains `diagnosis_nudged` (`harness/tooling.py:96`).
   - Rebuild derives it from `nudge` events
     (`storage/sqlite_store.py:1257-1258`, `:1281-1289`).
   - The early no-tool nudge persists a message but logs no `nudge` event
     (`harness/loop.py:142-148`); only the later budget nudge logs one
     (`harness/loop.py:164-170`). Resume can re-nudge.

7. **Env loading is inconsistent — partial**
   - `chaos_cli.py:30`, `:252` now calls `load_env()` alongside
     `run_benchmark.py:15-16`.
   - `score.py`, `replay.py`, and `db_cli.py` still rely on process env, and DB
     defaults are computed independently.

8. **Parallel public surfaces over the same singletons — open**
   - Lazy `harness.run_scenario`, `tools/schemas.py` shim, `harness/agent.py`
     re-exports, and singleton `TOOL_REGISTRY` remain
     (`harness/__init__.py:1-11`, `tools/schemas.py:1-31`, `tools/tool.py:242`).

9. **Stale or dead abstractions — open**
   - `common/xmlform.py` is still unused by the run flow.
   - `tools/schemas.py` remains a shim while the loop calls `registry.schemas()`
     directly (`harness/loop.py:121`).
   - `DESIGN.md:236` still attributes schema generation to `tools/schemas.py`.

10. **Docs drift — resolved in docs**
    - README now says injection failure aborts and scopes “read-only” to
      diagnostic tools; repairs are gated and separate
      (`README.md:624-626`, `README.md:60`, `README.md:699-705`).

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

Landing status: most named objects have landed, including `Checkpoint`,
`Message`, `Call`/`CallExecution`, `Journal`, `Verification`, `LLMOutcome`, and
the `RunStore`/`ChatClient` ports. The physical target layout has not: the repo
remains flat, `tools/` still mixes policy and adapters, and `TopologyPolicy`,
`RunExperiment`, `CommandRunner`, and a `Reporter` port do not exist yet.

## 7. Refactor sequence

Safety fixes went first, then the typed boundary objects, then the loop seam.
The legacy dict facade remains only as a compatibility adapter.

Landed:

1. Verify env symmetry, fixture fault-check env, fixture opt-in, evidence
   preservation on topology change, target validation, receipt rebuild,
   diagnosis attempt cap, and checkpoint-time lease renewal. Raw event payload
   redaction and background lease renewal remain partial (P0.4/P0.5).
2. One `Score` object used by loop, `score.py`, and `replay.py`; explicit
   scorer-version precedence; `verify_output` in records.
3. Streaming fallback visibility and redacted derived traces/exports; failed
   streaming attempts still have no dedicated event (P2.1).
4. `Scenario` is authoritative in runner, spec, loop, and gate policy; dicts are
   coerced only at compatibility boundaries and serialization.
5. `ExperimentSpec` / `RunContext`, `Budget`, `Call`, `LLMResponse`, `Result`,
   `Approval`, `Refusal`, `Message`, `Journal`, `Verification`, and `Checkpoint`
   carry the previously untyped boundary state.
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
11. Close the partial P0/P1/P2 items in §5 that are marked partial or open.

## 8. Review-driven design decisions

These decisions resolve the open design calls from the original review. They are
recorded here rather than in a new document to keep the doc surface small.

### 8.1 Gate receipt, not secret

The value named `approval_token` in the wire format is a gate receipt: a
protocol marker proving that diagnosis was accepted before repair. It is not a
secret and not human approval. Safety comes from canonical forbidden/unacceptable
repair policy and recorded `policy_violations`. The implementation gates on
harness-side state, records enough event data for rebuild, and redacts receipt-like
values from messages, derived traces, and UI output at write time. Raw SQLite event
payloads remain the rebuild authority: the `approval` event intentionally keeps
the receipt, while other raw tool args/results in event payloads are still a
partial gap (P0.5). Prompt mode adds an operator confirmation: it marks the run
`interactive=1`, returns the receipt only when the operator approves, and withholds
it on decline. Because the harness state is authoritative, a redacted or absent
wire value is accepted after resume instead of asking the model to guess a
suppressed value.

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
used by the loop, `score.py`, and `replay.py`.

### 8.6 Incremental OO migration

The target layout in §6 is a destination, not a big-bang rewrite. Beyond `Score`,
`ExperimentSpec`, `RunContext`, `RunStore`, and `ChatClient`, the hardening pass
landed `Budget`, `Call`, `Message`, `Journal`, `Verification`, `Checkpoint`,
`LLMOutcome`, and the typed `AgentLoop`. The legacy `run_scenario` facade and
limited `tools/` re-exports keep imports stable while remaining policy and
infrastructure modules move incrementally.
