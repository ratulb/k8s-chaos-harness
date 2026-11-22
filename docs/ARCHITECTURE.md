# Architecture draft (reverse-engineered)

Status: draft, reverse-engineered from the implementation at commit `134d2e9`.
Line references are relative to the repository root and reflect that commit.

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
  harness/agent.py:15  Agent
  harness/agent.py:32  build_agent()
    wires: LLMClient + TOOL_REGISTRY + Diagnosis + Repair
  harness/agent.py:42  run_scenario() facade
            |
            v
AGENT ORCHESTRATION
  harness/engine.py:19  run_scenario()
    - trace/artifact/event logging
    - AgentState restore/checkpoint
    - Message[] conversation
    - LLM loop
    - tool dispatch
    - verification
    - record + score + finish_run
            |
            v
DOMAIN / POLICY OBJECTS
  Scenario              tools/scenario.py:51
  LLMError              common/errors.py:7
  SystemPrompt          harness/prompt.py:9
  Message               harness/message.py:10
  Tool / Registry       tools/tool.py:61 / :124
  ToolOutcome           tools/tool.py:34
  AgentState            harness/tooling.py:26
  Diagnosis / Repair    harness/tooling.py:104 / :173
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
to OO. We have useful value objects at the edges, but the central loop still
coordinates mostly through dicts, globals, and `Any`.

## 2. Main ownership map

| Concern | Current owner | Comment |
| --- | --- | --- |
| Scenario definition + validation | `tools/scenario.py:51` | Good object, but the runner converts it back to `dict` at `run_benchmark.py:396`. |
| Cluster identity / topology | `tools/topology.py:516` | Discovers, freezes, hashes, resolves targets; still returns raw dicts. |
| Destructive safety | `tools/safety.py:29` | Enforces context/cluster/topology allow-lists. |
| Backend config | `llm/client.py:17` | `Backend` is typed, but engine receives it untyped. |
| Chat messages | `harness/message.py:10` | `Message` owns shape, `redact()`, and `truncate()`. |
| Prompt policy | `harness/prompt.py:9` | `SystemPrompt` renders topology-aware prompt. |
| Tool catalog | `tools/tool.py:235` | Singleton `TOOL_REGISTRY`; dynamic schemas; late-bound repair handlers. |
| Diagnosis/repair gates | `harness/tooling.py:104`, `harness/tooling.py:173` | Real domain policy; good seam. |
| Agent wiring | `harness/agent.py:32` | `build_agent()` is now a real mini composition root. |
| Orchestration loop | `harness/engine.py:19` | Still large; owns logging, loop, tools, verify, scoring handoff. |
| Persistence | `storage/sqlite_store.py:305` | Authoritative, event-log-first. |
| Scoring | `harness/engine.py:327`, `score.py:25`, `replay.py:44` | Duplicated in three places. |

### Layering assessment

```text
root CLIs / scripts
  -> harness/agent.py
  -> harness/engine.py
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
  scenario_obj.to_dict()          <-- typed object downgraded here
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
harness.agent.run_scenario()
  build_agent()
    LLMClient
    TOOL_REGISTRY
    Diagnosis
    Repair
        |
        v
harness.engine.run_scenario()
  restore/create AgentState
  restore/create Message[]
  SystemPrompt(topology).render()
  checkpoint
        |
        v
LLM LOOP
  Message[] -> dicts -> LLMClient.chat()
  response -> Message
  append to SQLite messages
  redact for event log
  checkpoint
        |
        +-- no tool calls + no diagnosis -> nudge
        +-- no tool calls + diagnosis -> stop
        +-- tool calls:
              parse args
              registry.get(name)
              store.begin_tool_call()
              Tool.run(args, ToolContext)
                diagnosis -> Diagnosis.execute()
                repair    -> Repair.execute() -> Tool.invoke_handler()
              ToolOutcome -> AgentState.apply()
              truncate tool result via Message.truncate()
              artifact + event + checkpoint
        |
        v
budget/stop condition
  budget_steps / budget_tokens / submitted / provider_error_* / agent_stopped
        |
        v
VERIFY + FINISH
  subprocess.run(verify_cmd)
  record = {...}
  score = {...}
  store.finish_run()
  export_all()
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

3. **Approval token lost on state rebuild**
   - Approval event omits `approval_token` at `harness/tooling.py:149-157`.
   - Rebuild expects it at `storage/sqlite_store.py:1123-1128`.
   - Cached `state_json` hides this until it is invalidated.

4. **Lease can expire during legitimate work**
   - 300s lease at `storage/sqlite_store.py:1063-1077`.
   - Repair readiness waits up to 180s at `tools/repairs.py:19-35`.
   - LLM retries can also be long at `llm/client.py:76-90`.
   - No renewal exists.

5. **Redaction asymmetry**
   - Traces redact approval tokens in prompt mode at `harness/engine.py:198`.
   - SQLite `messages` rows and `llm_response_full` store raw tool calls and
     responses.
   - Redaction is presentation-only, not a storage policy.

6. **Shell interpolation risk**
   - `shell=True` at `run_benchmark.py:53` and `harness/engine.py:281`.
   - `TARGET_NODE` / `TARGET_NODES` are interpolated without `shlex.quote` at
     `run_benchmark.py:459-460`.
   - Scenario validation checks only script existence at
     `tools/scenario.py:267-276`.

7. **Prompt-mode token contradiction**
   - Schema marks `approval_token` required for every repair at
     `tools/tool.py:217-231`.
   - Gate message says the token is suppressed at
     `harness/tooling.py:145-146`.
   - The model is implicitly asked to guess `approved:<run_id>`.

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
    - This works only because `build_agent()` reads the patched module global
      at call time.

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

5. **Score-version precedence is latent ambiguity**
   - `scores` primary key is `(run_id, scorer_version)` at
     `storage/sqlite_store.py:583-596`.
   - `INSERT OR REPLACE` at `storage/sqlite_store.py:1290-1294` prevents
     duplicate rows for the same version.
   - If multiple versions ever exist for one run,
     `storage/sqlite_store.py:1339-1342` keys only by `run_id` with no
     `ORDER BY`, so precedence is arbitrary.
   - Currently only `legacy-record-v1` is written.

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

10. **Docs drift**
    - README says injection failure does not stop the agent, but the runner
      aborts at `run_benchmark.py:170-176`.
    - README §11 calls tools “read-only” while repairs execute.

## 6. Draft target architecture

```text
domain/
  Scenario                owns budgets, repair policy, targets
  TopologyPolicy          owns hash, capability, target selection
  ExperimentSpec          scenario + backend + guard + run_env + config_hash
  Message                 chat shape + redaction + truncation
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
  build_agent()
  chaos_cli.main()
```

## 7. Recommended refactor sequence

The next work should not be “move more code.” It should replace the unresolved
boundaries:

1. Make `Scenario` authoritative end-to-end.
2. Introduce `ExperimentSpec` / `RunContext` instead of dict bags.
3. Add `RunStore` and `ChatClient` ports.
4. Collapse the two `run_scenario` APIs into one typed
   `AgentLoop.run(spec) -> RunResult`.
5. Create one `Score` object used by engine, `score.py`, and `replay.py`.
6. Move final verification into a runner that receives the same frozen env as
   baseline/injection.
7. Make SQLite the only writer of derived traces/exports.
8. Fix the P0 safety gaps before the re-pilot.
