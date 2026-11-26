# LangChain / LangGraph Integration Design

**Branch:** `langchain`  
**Status:** Draft design for exploration; implementation may be partial  
**Date:** 2026-11-26  
**Scope:** Evaluate and design LangChain/LangGraph use in `k8s-chaos-harness` for learning and controlled experimentation

## 1. Why this document exists

This harness already implements a custom agent loop, tool dispatch, gate, state machines, checkpointing, SQLite event log, replay, and scoring. The question is where LangChain/LangGraph could fit, what we would learn, and what must remain hand-written. The goal is not to "add a framework" for its own sake. The goal is to learn where a framework helps, where it conflicts with harness safety/comparability invariants, and how to run that experiment without weakening the benchmark.

This is an end-to-end design and assessment before implementation. We may implement only selected parts.

## 2. Current system constraints

The design must preserve the invariants described in `docs/ARCHITECTURE.md`, `docs/STATE_MACHINE.md`, and `docs/call_flow.md`.

| Invariant | Current owner | Design implication |
| --- | --- | --- |
| Cluster lifecycle is harness-owned | `run_benchmark.py`, `cluster/*.sh`, `faults/*.sh`, `harness/verification.py` | LangGraph must not reset, inject, verify, or clear faults. |
| Destructive safety boundary | `tools/safety.py`, `chaos_cli.py`, topology freeze/hash | Framework cannot bypass guard, fixture opt-in, or topology mismatch rules. |
| Model sees narrow evidence | `harness/prompt.py`, `tools/tool.py` | Tool schemas/prompts must remain equivalent; no scenario truth/policy leakage. |
| Diagnosis/repair gate is authoritative | `harness/tooling.py`, `harness/gate_runtime.py`, `harness/machines/gate.py` | LangGraph edges may observe gate state, but cannot authorize repairs. |
| Intent-before-mutation and unknown-effect handling | `harness/tool_runtime.py`, `harness/call.py`, `storage/sqlite_store.py` | Mutating tool replay from framework checkpoint is forbidden. |
| Append-only SQLite event log is truth | `storage/sqlite_store.py`, `harness/journal.py` | LangGraph checkpoint/state is not authoritative for scored runs. |
| Canonical scoring reads folded state | `common/scoring.py` | `gate-path-v1` inputs and semantics must remain unchanged. |
| Reproducible provenance | `llm/client.py`, `llm/registry.py`, `llm/response.py`, run records | Backend/fallback/attempt/usage/raw response provenance must remain auditable. |

## 3. Fit assessment

This section maps potential LangChain/LangGraph capabilities to the current harness and records our assessment.

| Capability / idea | Fit | Assessment | Decision |
| --- | --- | --- | --- |
| Agent loop as LangGraph `StateGraph` | Strong | The hand-rolled loop in `harness/loop.py` is close to a graph: LLM call, tool dispatch, nudge, terminal routing. This is the clearest learning target. | Primary implementation target if we proceed. |
| Tool schemas as LangChain tools / Pydantic args | Medium | Useful for learning schema ergonomics, but schema conversion can drift. Existing `ToolRegistry` already defines authoritative schemas and safe execution. | Benchmark path should derive/pass through existing registry schemas. |
| Chat model abstraction / `with_fallbacks` | Medium | Useful for learning provider abstractions, but current `LLMClient` carries health sorting, fallback provenance, retries, and raw artifacts. | Parity mode wraps existing `LLMClient`; native LangChain model mode is a deferred learning spike. |
| Prompt templates | Low/medium | `SystemPrompt` is small and deterministic; templating adds little immediate value. | Optional follow-up, not core. |
| Diagnosis gate as interrupts/subgraph | Medium, risky | Gate is already an explicit state machine with dedupe/violations. `interrupt()` maps conceptually to approval, but re-run semantics are risky. | Keep `GateRuntime` authoritative; `interrupt()` only as isolated non-scored spike. |
| SQLite checkpointer / resume | Medium, risky | Good learning comparison, but harness already has event-sourced resume and unknown-effect safety. | Learning experiment only; SQLite event log remains authoritative. |
| LangSmith/callback observability | Medium | Useful for tracing UI, but exports sensitive data and is not authoritative. | Optional debug adapter, off by default, never authoritative. |
| Batch orchestration / Send | Weak | Existing batch/run separation is simple and adequate. | Do not prioritize. |
| Replay/time-travel | Poor fit | Harness replay is deterministic evidence rendering; framework replay may re-execute tools. | Keep existing replay. |
| Fault injection/reset/verification/scoring as graph nodes | Poor/dangerous | These are trust-boundary operations and benchmark credibility surfaces. | Explicitly out of scope. |

**Assessment:** porting only the agent loop is the best learning/value tradeoff. Safety, gate, evidence, and scoring should remain hand-written.

## 4. Naive adoption conflicts and tradeoffs

This section captures the "why not just use LangGraph?" reasoning. It is intentionally explicit because the project goal includes learning LangChain/LangGraph, and the easiest way to learn the wrong lesson is to adopt framework defaults without seeing where they conflict with this harness.

### 4.1 Conflicts if adopted naively

| Area | Current harness | LangChain/LangGraph default/tendency | Conflict |
|---|---|---|---|
| **State authority** | SQLite append-only events are truth; `RunRuntime`/`GateRuntime`/`ToolLedger` fold events; `runs.state_json` is a rebuildable cache (`storage/sqlite_store.py`, `harness/*_runtime.py`) | Checkpointers (`MemorySaver`/`SqliteSaver`) make graph state resumable/replayable | Framework checkpoint can diverge from the event log or become an accidental second source of truth. |
| **Crash/resume safety** | Mutating tool intent is recorded before execution; unfinished mutating call becomes `unknown_effect`; resume triage avoids unsafe replay (`harness/tool_runtime.py`, `harness/run_runtime.py`) | Resuming/checkpoint replay can re-run node work from a boundary | A resumed graph could re-execute a mutating repair like `restart_member`/`member_remove_readd`. Unsafe for cluster state and benchmark credibility. |
| **Gate/safety authority** | `Diagnosis`/`Repair` + `GateRuntime` enforce diagnosis cap, receipt, forbidden/unacceptable repairs, violations (`harness/tooling.py`, `harness/gate_runtime.py`) | Graph edges/interrupts/tool nodes often drive control flow directly | LLM/framework could bypass or weaken gate semantics unless every action goes through existing gate/runtime APIs. |
| **Tool execution** | Sequential tool calls, typed `ToolOutcome`, truncation/redaction, artifacts, audit events (`harness/call.py`, `tools/tool.py`) | Prebuilt `ToolNode`/agents are convenient; may parallelize and format errors/messages their own way | Parallel/out-of-order execution breaks mutating ordering; convenience wrappers can skip intent/audit/provenance; schema conversion can drift. |
| **LLM provenance** | `LLMClient` records fallback chain, selected backend, attempts, usage, raw response/error artifacts (`llm/client.py`, `llm/response.py`) | Model wrappers/fallbacks can hide which model actually answered and why | Loses comparability/debug evidence; "same run config" becomes ambiguous. |
| **Message serialization** | Canonical dict messages stored/redacted in SQLite (`harness/message.py`) | LangChain `AIMessage`/`ToolMessage` conversion/normalization | Subtle drift in tool-call args/ids/content can change model behavior and break parity. |
| **Trust boundary** | Harness owns reset/inject/fault-check/verify/scoring; model gets narrow read-only+gated tools (`run_benchmark.py`, `harness/verification.py`, `common/scoring.py`) | LangGraph can orchestrate end-to-end workflows | Tempting but dangerous to put destructive lifecycle or scoring in graph nodes. |
| **Observability/privacy** | Local SQLite/replay/doctor; redacted artifacts | LangSmith/callback tracing often exports externally | Sensitive cluster/tool outputs can leak unless explicitly controlled. |
| **Comparability** | Fixed scorer `gate-path-v1`, config hash, provenance fields | Framework adds serialization/runtime/retry behavior | Native vs LangGraph runs become apples-to-oranges unless isolated as an experiment variable. |

### 4.2 What we are deciding to incorporate

Per this design (**Option C, parity-first**):

- **Incorporate:** `agent_impl=native|langgraph`; a new `LangGraphAgentLoop` that replaces only agent-loop orchestration (the `harness/loop.py` role), behind the same `run(spec, ctx) -> RunResult`.
- **Reuse as authority:** `Journal`, `Checkpoint`, `Budget`, `AgentState`/`Diagnosis`/`Repair`, `GateRuntime`, `RunRuntime`, `ToolLedger`, `ToolRegistry`, `SQLiteStore`, `Verification`, `common/scoring.py`.
- **Parity constraints:** registry JSON schemas passed through (no schema drift), no prebuilt `ToolNode`/ReAct agent, sequential tool execution, dict messages canonical in parity mode, existing `LLMClient` parity adapter for provenance, LangGraph checkpointer not authoritative/read on resume.
- **Explicitly not incorporating now:** outer lifecycle graph, gate replacement, LangSmith by default, native LangChain model/fallback stack as default, `interrupt()` approval flow, LangGraph checkpoint as resume authority.

### 4.3 What we gain

- **Learning, safely:** StateGraph mental model - nodes, conditional edges, routing state, checkpoint concepts - while seeing exactly which parts cannot be delegated.
- **Measured answer:** H1 parity tests/report tell us what LangGraph actually removes from `loop.py` vs what must stay hand-written. A "little removed" result is still useful.
- **Experiment scaffolding:** `agent_impl` becomes a controlled variable for future A/Bs; sandbox crash-lab/native-LC spikes can teach idiomatic LangGraph without contaminating scored runs.
- **Potential clarity:** Orchestration may become easier to visualize/reason about if parity holds.

### 4.4 What we lose / cost

- **Not full idiomatic LangGraph in scored path:** Parity mode intentionally avoids `ToolNode`, prebuilt agents, `add_messages` reducers, native checkpointer authority, and LangSmith. We learn constrained graph orchestration, not the whole framework surface.
- **More code/maintenance:** Second loop implementation, tests, migration (`runs.agent_impl`), pinned optional deps, API-churn risk.
- **No capability/score gain expected:** This is for learning/comparison; likely no improvement to `strict_loop_success`, and initial parity bugs are possible.
- **Small overhead/complexity:** Extra abstraction and graph runtime; measured only after implementation.

### 4.5 Bottom line

Naive LangChain/LangGraph adoption conflicts with this harness’s safety, audit, and scoring model. The design incorporates only a narrow, observable orchestration layer so we can learn LangGraph concepts without making the framework authoritative over cluster mutations, gate decisions, or benchmark evidence.

## 5. Goals, non-goals, and hypotheses

### Goals

1. Add a **learning-grade, benchmark-safe** LangGraph implementation of the agent loop.
2. Make the framework an experimental variable, comparable against the native loop.
3. Preserve existing safety, gate, event log, scoring, replay, and doctor behavior.
4. Capture lessons: where framework abstractions help, where they hide needed provenance/safety detail.

### Non-goals

1. Rewriting `run_benchmark.py` lifecycle as a LangGraph app.
2. Replacing `GateRuntime`, `RunRuntime`, `ToolLedger`, or `common/scoring.py`.
3. Using LangGraph checkpoint as authoritative resume state for scored runs.
4. Sending cluster traces to LangSmith by default.
5. Changing scenario policy, tool names, prompt semantics, or score definition for comparability.

### Hypotheses

The design intentionally constrains LangGraph in parity mode. That means the experiment can show two different things:

- **H1 - Port correctness (parity mode):** Given the same model outputs, the LangGraph loop produces the same authoritative event stream/folded result as native. Any difference is a port bug. With parity adapter, registry-derived schemas, SQLite authority, and minimal graph state, near-zero behavioral difference is expected and acceptable.
- **H2 - Framework effect (native LangChain mode, deferred):** LangChain model/message/tool-binding layers change outcomes. These runs are **non-comparable** and must never be pooled into headline aggregates; they may be compared as a separate experimental arm.

The likely H1 finding is that LangGraph removes little from `harness/loop.py` while teaching routing/checkpoint/interrupt concepts in sandbox experiments. That is a valid result; the doc should make it measurable, not embarrassing.

### Success / stop criteria

The experiment is successful if it produces a measured report, not if it "proves LangGraph is better".

| Criterion | Evidence |
| --- | --- |
| Port correctness | Record-and-replay parity test passes: identical event types/order/payloads (timestamps stripped), identical final record/folded score inputs, identical provider request payloads after normalization. |
| Crash safety | Kill-mid-run test shows mutating unknown-effect parity and no mutating replay. |
| Comparability discipline | `agent_impl` grouped in score/reporting; langgraph excluded from headline by default; config hash includes impl/framework metadata. |
| Learning value | Report contains: LOC/concepts removed vs hand-written, provenance gaps, parity failures and causes, framework bugs hit, wall-clock overhead/step, and what had to stay hand-written. |
| Stop/pivot | If parity requires bypassing gate/tool/runtime invariants, or if framework churn/dep weight exceeds value, keep native default and retain only sandbox learnings. |

## 6. Options considered

### Option A - Full rewrite around LangGraph

Reimplement run lifecycle, tools, gate, checkpoint, and possibly scoring integration as graph nodes/subgraphs.

**Rejected.** Highest migration cost and highest risk to safety/crash semantics. It would blur the trust boundary that makes the harness credible.

### Option B - Adapter-only LangChain chat models

Keep native loop; add LangChain model adapters behind `ChatClient`/LLM client.

**Good first step, insufficient as end goal.** It teaches provider abstraction but does not exercise LangGraph state/edges/checkpoint/interrupt concepts we want to learn.

### Option C - Experimental agent-loop port behind existing contract (recommended)

Add `agent_impl=native|langgraph`. Keep `native` default. Implement `LangGraphAgentLoop.run(spec, ctx) -> RunResult` with the same contract and same authoritative runtimes.

**Selected.** Smallest blast radius, preserves comparability, and directly targets the core LangChain/LangGraph learning question.

### Option D - LangGraph for outer run lifecycle, native inner loop

Model reset/inject/verify/agent as outer graph.

**Deferred.** Reconsider only after Option C has: (a) scripted record-and-replay parity green, (b) disposable-cluster A/B on at least one control and one fault scenario with no unexplained strict-score/end-reason delta, (c) mutating crash/unknown-effect parity test green, and (d) zero doctor drift across repeated runs.

## 7. Recommended architecture

### 7.1 Boundary

```text
run_benchmark.py / chaos_cli.py
  AUTHORITATIVE: preflight, reset, baseline, inject, fault check, verify, scoring/export
  selects agent_impl=native|langgraph

Agent (harness/agent.py)
  dispatches:
    native    -> AgentLoop            (existing, default)
    langgraph -> LangGraphAgentLoop   (new, experimental)

LangGraphAgentLoop  (ORCHESTRATION ONLY)
  may: call LLM, route on observed state, invoke tool wrappers, emit nudges
  must not: authorize repairs, mutate cluster lifecycle, own truth/scoring/resume

Authoritative runtimes reused by both loops
  Journal / Checkpoint / Budget
  RunRuntime / GateRuntime / ToolLedger
  Diagnosis / Repair / Call / ToolRegistry
  SQLiteStore event log + projections

Trust boundary arrows:
  LangGraph node -> authoritative runtime: COMMAND (through existing APIs only)
  authoritative runtime -> LangGraph node: OBSERVE (state/facts/results only)
  model/tool output -> gate/repair: UNTRUSTED until validated by Diagnosis/Repair/GateRuntime
```

### 7.2 New/changed modules

| Path | Change | Purpose |
| --- | --- | --- |
| `harness/agent.py` | modify | Dispatch by `spec.agent_impl`. |
| `harness/engine.py` | modify | Pass `agent_impl`; construct selected loop. |
| `harness/spec.py` | modify | Add `agent_impl`, `agent_impl_version`, framework metadata. |
| `harness/loop.py` | keep | Native baseline; no behavior change. |
| `harness/langgraph_loop.py` | add | Graph orchestration implementing `run(spec, ctx) -> RunResult`. Start here; split only if it grows. |
| `run_benchmark.py` | modify | `--agent-impl`, config hash, run metadata, resume mismatch refusal. |
| `chaos_cli.py` | modify | Pass-through `--agent-impl` on run/batch surfaces as needed. |
| `storage/sqlite_store.py` | modify | Add `runs.agent_impl` column (default `native`) + migration; include in projections/exports/doctor context. |
| `requirements-langgraph.txt` | add | Pinned optional deps; lazy import only when `agent_impl=langgraph`. |
| `tests/test_langgraph_*.py` | add | Schema, request-equivalence, parity, resume/crash, config hash. |

Optional split files only after spike proves need: `langgraph_state.py`, `langgraph_tools.py`, `langgraph_llm.py`, `langgraph_callbacks.py`.

### 7.3 Graph state

LangGraph state is orchestration-only. Authoritative objects are not serialized into graph checkpoints.

```python
from typing import TypedDict, NotRequired
from typing import Any

class AgentGraphState(TypedDict):
    # Canonical conversation for the current graph invocation.
    # Parity mode: raw OpenAI-style dict messages.
    # Native-LC mode (deferred): LangChain AnyMessage allowed.
    messages: list[dict[str, Any]]

    # Routing-only signals; derived from authoritative runtimes after each node.
    llm_error: bool
    end_reason: str
    last_tool_call_count: int
    should_continue: bool
```

**No duplicated counters.** `steps`, `tokens_used`, `diagnosis_nudged`, gate phase, receipt, and terminal reason live in `Budget`/`AgentState`/`RunRuntime`/`GateRuntime`. Routing functions read authoritative objects; tests assert graph routing signals agree after each node.

**Reducer:** none required for parity mode because each node returns a complete updated state dict; do not use `add_messages` until native-LC mode needs it, and then pin merge-by-id behavior explicitly.

Runtime context held by loop instance/closures (never checkpointed):

```python
@dataclass
class GraphRuntime:
    spec: ExperimentSpec
    ctx: RunContext
    store: RunStore
    journal: Journal
    run: RunRuntime
    state: AgentState        # diagnosis/gate/repair state
    budget: Budget
    tool_ledger: ToolLedger
    checkpointer: Checkpoint
    diagnosis_executor: Diagnosis
    repair_executor: Repair
    registry: Any
```

### 7.4 Graph topology

```text
START
  -> llm_node
  -> route_after_llm

route_after_llm:
  llm_error/provider_error                -> finalize
  no_tool_calls && diagnosis_missing
    && budget.can_continue_without_tool_call -> nudge_node -> llm_node
  no_tool_calls                           -> finalize
  tool_calls                              -> tools_node -> post_tools_node -> route_after_tools

route_after_tools:
  run.terminal || budget.exhausted        -> finalize
  else                                    -> llm_node

finalize -> END
```

#### Node responsibilities

| Node | Responsibility | Reused component |
| --- | --- | --- |
| `llm_node` | Call model through parity adapter; append assistant message; persist `llm_requests`/raw response/assistant event/message; update budget tokens; capture checkpoint. | `LLMOutcome`/adapter, `Journal`, `Message`, `Budget`, `Checkpoint` |
| `nudge_node` | Emit diagnosis nudge as user message when no tool calls and diagnosis missing; record nudge in `AgentState`/budget; persist; capture checkpoint. | `Budget`, `AgentState.record_nudge`, `Journal`, `Checkpoint` |
| `tools_node` | Execute **all tool calls from the current assistant turn sequentially**, in order, through existing `Call`/`Tool`/runtime path. Persist tool result messages/events/artifacts. No LangChain `ToolNode`. | `Call`, `ToolRegistry`, `ToolLedger`, `Diagnosis`, `Repair`, `Journal` |
| `post_tools_node` | Apply diagnosis-deadline budget nudge if needed; capture checkpoint; compute routing signals. | `Budget`, `Checkpoint`, `RunRuntime` |
| `finalize` | Map outcome to `RunRuntime` terminal agent event; no verification here. | `RunRuntime`, `AgentState`, `Budget` |

#### Ordering and accounting rules

The native loop is the reference. The LangGraph loop must preserve these semantics:

1. **One LLM request per `llm_node` invocation**. `budget.start_step()` happens before request, same as native.
2. **Multi-tool-call turns:** if one assistant message contains multiple tool calls, `tools_node` executes them sequentially in message order within that turn. A diagnosis followed by repair in the same turn is allowed only if existing gate/tooling allows it after the diagnosis call updates state; parity test must pin this case.
3. **No `ToolNode`:** LangChain's prebuilt `ToolNode` may parallelize and has its own error/message conventions. We bypass it intentionally. We lose its convenience; we keep ordering, intent-before-mutation, and audit semantics.
4. **Nudge accounting:** "no tool call + diagnosis missing" nudge uses existing `Budget.can_continue_without_tool_call()` guard and `AgentState.record_nudge(step=budget.steps, kind="no_tool")`; diagnosis-deadline nudge uses `Budget.should_nudge_diagnosis()` and `kind="budget"`. Nudges append user messages and persist like native; they do not create fake LLM steps.
5. **Checkpoint capture points:** capture after each persisted mutation boundary - initial state, after assistant message commit, after nudge persist, after each tool execution commit (or after tool batch with same per-call commits as native), and final capture before return. `state_seq` monotonicity and snapshot shape are parity-tested.
6. **Lease renewal:** `LeaseRenewer` remains active around the graph run; `Checkpoint.capture()` renews lease as today. No long node may skip capture/renewal boundaries beyond existing behavior.

### 7.5 Tool wrapping strategy

#### Benchmark-safe path (parity mode)

Use existing `TOOL_REGISTRY.schemas()` as the tool schema payload sent to the model. Do **not** regenerate schemas through Pydantic/LangChain conversion in parity mode; that avoids enum/default/additionalProperties/required drift.

Execution wraps existing `Call`/`Tool`/runtime path, not raw handlers:

1. Normalize tool call input into `Call`.
2. `ledger.for_call(...)` and `store.begin_tool_call(...)` - intent recorded before mutating execution.
3. Execute through `Tool.run(args, ToolContext(...))` / `Call.invoke(...)`.
4. Commit through existing `CallExecution.commit(...)` semantics: truncation, redaction, artifact, tool_call finish, message persist, event.
5. Return tool message dict for conversation.

LangChain `StructuredTool` objects are optional schema carriers in parity mode; if used, they must be built from registry JSON schema and never become the execution authority. Mode 2 may use native LangChain tool binding, but is deferred and non-comparable.

#### Rules

- Preserve names, descriptions, parameter schemas, enums, required fields, defaults.
- Preserve `ToolOutcome` status/result_code semantics.
- Preserve redaction/truncation and artifact storage.
- No `approval_token`/receipt fields exposed to model.
- No raw shell tool, no dynamic tool registration from model.
- Mutating unknown-effect guard: before executing a mutating tool, if ledger indicates unresolved started mutating call, refuse execution and follow existing abort/escalation path.

### 7.6 LLM strategy

#### Mode 1 - Parity adapter (default, required for H1)

Wrap existing `LLMClient`/`LLMOutcome`. Convert stored dict messages to provider request as today; return response as dict/`AIMessage`-compatible normalized structure but persist canonical dict messages.

**Preserves:** health-aware ordering, fallback chain, retries, raw response capture, `llm_requests` provenance, error classification, streaming callback behavior.

**Message contract:** SQLite/stored dict messages are canonical. Any LangChain message conversion is lossless for fields used by the loop (`role`, `content`, `tool_calls`, `tool_call_id`, `name` where relevant). Test with captured real provider responses, not only synthetic fakes.

#### Mode 2 - Native LangChain models (deferred, H2)

Use `init_chat_model`/provider chat models with `bind_tools`, optionally `with_fallbacks`. Requires callbacks to capture provenance. Runs are marked `non_comparable=true` and excluded from headline aggregates.

| Concern | Parity adapter | Native LangChain |
| --- | --- | --- |
| Fallback behavior | Existing `LLMClient` | LangChain `with_fallbacks`; may differ |
| Provenance | Existing `llm_requests` fields | Requires callbacks; easy to lose detail |
| Streaming | Existing on_chunk | Provider-specific |
| Comparability | High | Low/experimental |
| Learning value | Medium | High |

## 8. RunResult contract

`LangGraphAgentLoop.run()` must return the same `RunResult` as `AgentLoop.run()`. This table is the parity contract.

| Field | Source of truth | Set by | Notes |
| --- | --- | --- | --- |
| `record` | final run record dict | finalize + verification path in loop | Must include existing keys consumed by `SQLiteStore.finish_run`, scorer, exports. |
| `record.run_id`, `scenario`, `backend` | spec/ctx | loop | unchanged. |
| `record.reset_ok/baseline_ok/inject_ok/fault_present` | `RunContext`/`RunRuntime` | setup/finalize | unchanged. |
| `record.diagnosis*`, `approval_granted`, `repair*`, `policy_violations`, `forbidden_reason` | `AgentState`/`GateRuntime` | diagnosis/repair runtimes | unchanged. |
| `record.verify_ok`, `verify_output` | `Verification` | post-graph verification | verification remains outside graph. |
| `record.steps`, `tokens_used` | `Budget` | llm/tool loop | must match native accounting. |
| `record.trace` | `trace_path` | loop | unchanged. |
| `record.end_reason` | agent-loop stop reason | finalize mapping | see table below. |
| `score` | `common/scoring.Score.from_record` | finalize | same scorer/version `gate-path-v1`. |
| `end_reason` | same as `record.end_reason` | finalize | agent-loop stop reason; not folded verification reason. |
| `verify_ok`, `verify_output` | `Verification` | post-graph verification | unchanged. |
| `state` | `Checkpoint.capture()` final snapshot | finalize | includes gate, run, tools, gate_path. |
| `result` | `Result.success(record)` / failure as native | finalize | unchanged. |

### Finalize/end-reason mapping

| Condition | `run.*` terminal event | `end_reason` |
| --- | --- | --- |
| LLM/provider failure, all candidates exhausted | `provider_error_all` | existing `LLMOutcome.end_reason` (`provider_error_*`/`error`) |
| LLM failure, non-provider-all | `agent_terminal` | existing error reason |
| Budget exhausted | `agent_done` with gate/budget context | `budget_steps` or `budget_tokens` from `Budget.exhausted_reason()` |
| Gate/agent terminal by policy | `agent_done` or `agent_terminal` as native | native-equivalent reason (`submitted`, `agent_stopped`, gate reason) |
| Normal no-tool-call completion | `agent_done` | `submitted` if diagnosis submitted else `agent_stopped` |

`verify_ok`/`verify_failed` remain post-loop `RunRuntime` events and affect folded scoring reason as today; they do not overwrite the stored agent-loop `end_reason` except through existing record semantics.

## 9. Checkpointing, resume, interrupts, and crash safety

### Authoritative path

SQLite is authoritative:

- `trace_events` canonical machine events.
- `messages`, `tool_calls`, `llm_requests`, `run_artifacts`.
- `runs.state_json` cache rebuilt by `Checkpoint.capture()` / store rebuild.
- `RunRuntime.from_events`, `GateRuntime.from_snapshot/events`, `ToolLedger.from_events`.

### LangGraph checkpointer

Default `none`/`MemorySaver` for in-process execution only; **never read on resume**. Optional `SqliteSaver` is a sandbox learning spike, not a scored-run authority. Scored runs must set `langgraph_checkpointer=none|memory` and resume only from SQLite.

### Resume flow

On resume, `LangGraphAgentLoop` starts a **fresh graph invocation** with state rebuilt from SQLite:

1. Load messages from SQLite.
2. Rebuild `AgentState`, `RunRuntime`, `ToolLedger`, `Budget` from snapshot/events.
3. Reconcile unfinished tool calls exactly like native resume: started mutating -> unknown effect/abort path; read-only intended/started -> supersede as appropriate.
4. Pass rebuilt dict messages into initial graph state.
5. Continue orchestration; never replay tool side effects.

`steps`, `tokens_used`, and `diagnosis_nudged` come from rebuilt `Budget`/snapshot, not graph checkpoint.

### Crash-safety rule

If a mutating tool intent exists without terminal outcome, the run is `unknown_effect` and must follow existing abort-and-retry/triage. LangGraph node retries/checkpoint restores must check tool ledger before any mutating execution.

### Interrupts

Deferred. Current approval is synchronous stdin with reporter pause/resume through `Diagnosis`. LangGraph `interrupt()` would require a dedicated side-effect-free approval node and persisting the approval decision as a gate event through `GateRuntime` before resume; resuming an interrupt re-runs the node from its start, so side effects in that node are dangerous. Do not prototype inside `tools_node`.

## 10. Observability and privacy

| Signal | Source of truth | LangGraph addition |
| --- | --- | --- |
| Run events | `trace_events` via `Journal` | Optional callback mirror, debug only |
| Messages/tool calls/LLM requests | SQLite tables | No replacement |
| Raw responses/errors | `run_artifacts` | No replacement |
| Replay | `replay.py` from stored evidence | No replacement |
| Debug tracing | local logs/DB | Optional LangSmith, off by default, redacted |

**LangSmith rule:** disabled by default. The harness must explicitly disable tracing env vars for scored runs unless an explicit opt-in flag/env is set. Add a test asserting scored runs do not emit LangSmith tracing when env vars are present but opt-in is absent. Enabling requires warning that prompts/tool outputs may be exported externally.

## 11. Config, schema, and provenance

### CLI/config

- `run_benchmark.py --agent-impl native|langgraph` default `native`.
- `chaos_cli.py run ... --agent-impl ...` pass-through.
- Resume: stored `agent_impl` must match requested/implicit impl; mismatch is refused and **not bypassable by `--force-resume`** (like topology mismatch, unlike config-hash mismatch).
- Optional env: `CHAOS_AGENT_IMPL`, `CHAOS_LANGGRAPH_LLM_MODE=parity` (native-lc deferred), `CHAOS_LANGGRAPH_CHECKPOINTER=none|memory`.

### Config hash

Include in `config_hash_input` when relevant:

```python
"agent_impl": args.agent_impl,
"agent_impl_version": "langgraph-v1",      # when langgraph
"langgraph_llm_mode": "parity",            # when langgraph
"langgraph_checkpointer": "none|memory",   # when langgraph
"graph_definition_hash": "...",            # hash of compiled node/edge topology or static definition
"framework_versions": {                    # resolved at run creation, not scoring time
    "langgraph": "...",
    "langchain-core": "...",
}
```

### SQLite

Add additive migration:

- `runs.agent_impl TEXT NOT NULL DEFAULT 'native'`

Optional later: `runs.agent_impl_meta_json TEXT`. State-only storage is insufficient for score grouping/filtering.

### Comparability policy

Resolved: `langgraph` runs are excluded from headline aggregates by default. Score/reporting must group by `agent_impl`. Opt-in inclusion requires explicit flag (e.g. `--include-agent-impl langgraph` or `--agent-impl all`), never default.

## 12. Testing and verification

### Keep existing suites green

```bash
python3 scripts/gen_state_diagrams.py --output docs/STATE_MACHINE.md
python3 -m compileall -q .
python3 -m unittest discover -s tests -q
git diff --check
```

### New tests

| Test | Purpose |
| --- | --- |
| `tests/test_langgraph_tool_schema.py` | Wrapped/tool payload schema matches `TOOL_REGISTRY` exactly: names, params, required, enums, defaults, rendered JSON. |
| `tests/test_langgraph_request_equivalence.py` | Scripted fake LLM; native and langgraph produce identical normalized request payloads (messages + tools). |
| `tests/test_langgraph_parity.py` | Record-and-replay/scripted fake: identical event type/order/payloads (timestamps stripped), identical final record/score inputs. |
| `tests/test_langgraph_nudges.py` | No-tool diagnosis nudge, deadline nudge, nudge accounting/step counts match native. |
| `tests/test_langgraph_multi_tool_turn.py` | One assistant message with multiple tool calls executes sequentially; diagnosis+repair same-turn case pinned. |
| `tests/test_langgraph_resume.py` | Resume rebuilds from SQLite; unfinished read-only superseded; mutating unknown effect not replayed; agent_impl mismatch refused. |
| `tests/test_langgraph_config_hash.py` | `agent_impl` and framework metadata affect config hash; framework versions captured at run creation. |
| `tests/test_langgraph_langsmith.py` | Scored run forces tracing off unless explicit opt-in. |

### Sandbox experiments (not scored benchmark path)

1. **Crash lab:** fake mutating tool appends line to file; `SqliteSaver`/memory; kill between nodes and mid-node; observe replay behavior. No cluster required.
2. **Idiomatic LangGraph mini-loop:** `ToolNode`/`bind_tools`/`add_messages`/prebuilt agent in a scratch script to learn framework ergonomics before constraining it.
3. **Native-LC mode spike:** after H1 green, measure provenance/serialization drift.

### Manual disposable-cluster A/B

Run at least 5 runs per cell before drawing conclusions; seed is metadata, not sampling control.

1. Control-noise scenario, same backend, native vs langgraph.
2. One injected fault scenario, compare strict score/steps/tokens/end reason/gate path.
3. Kill process mid-run during read-only tool and during mutating tool; compare resume/unknown-effect behavior. This is a named acceptance criterion for crash safety.

## 13. Rollout plan

1. **Design review** - this document.
2. **Schema/provenance harness** - request-equivalence and tool-schema tests before graph implementation.
3. **Parity implementation** - `LangGraphAgentLoop` with parity LLM adapter, registry schemas, sequential tools, SQLite authority. No prebuilt agent.
4. **Offline parity tests** - fake/scripted client, record-and-replay, nudges, multi-tool turn, resume/crash.
5. **Disposable-cluster A/B** - native vs langgraph, repeated runs, grouped by `agent_impl`.
6. **Report** - H1 result, LOC/concept diff, provenance gaps, parity failures, crash results, overhead, hand-written remainder.
7. **Decide** - keep as experimental `agent_impl`, promote specific adapters/patterns, or stop.

## 14. Risks and mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| LangGraph checkpoint replays mutating tool | Duplicate/unsafe cluster mutation | SQLite authoritative; mutating intent/unknown-effect guard; no mutating replay from graph checkpoint. |
| Provenance loss in model fallback | Benchmark incomparability | Parity adapter wraps existing `LLMClient`; record `llm_requests` unchanged. |
| Prompt/tool serialization drift | Behavior changes mistaken for model quality | Raw request/response artifacts; request-equivalence tests; compare native vs langgraph on same inputs. |
| Message round-trip drift | Subtle model behavior change | Dict messages canonical in parity mode; lossless-field contract; captured-response tests. |
| Framework API churn | Maintenance burden | Pin versions; isolate optional deps; keep native path default. |
| Gate bypass through graph edge | Safety/policy regression | GateRuntime/Diagnosis/Repair remain sole authority; graph routes on observed state only. |
| LangSmith data export | Sensitive trace leakage | Off by default; force-disable env in scored runs unless opt-in; redaction; docs warning. |
| Added dependency weight | Slower setup/CI | `requirements-langgraph.txt`; lazy import; native path has no new required deps. |
| Framework exceptions look like model failures | Misleading scores | Map GraphRecursionError/validation/state errors to `framework_error`, excluded from headline. |
| LangGraph does little in parity mode | Low learning payoff | Expected; measure it. Use sandbox experiments for idiomatic/checkpointer/interrupt learning. |

## 15. Resolved decisions

| Question | Decision | Rationale |
| --- | --- | --- |
| Packaging | `requirements-langgraph.txt`, pinned versions, lazy import; native path needs no LangChain/LangGraph deps. | Lowest friction; preserves native CI/runtime. |
| DB schema | Add `runs.agent_impl TEXT NOT NULL DEFAULT 'native'` now. | Needed for score grouping/filtering and doctor/reporting. |
| Native LangChain LLM mode | Defer until H1 parity is green; then separate non-comparable spike. | Avoids mixing port bugs with framework behavior changes. |
| Interrupts | Defer. Separate non-scored spike with side-effect-free approval node. | Current stdin approval path is synchronous; interrupt resume re-runs node. |
| CLI scope | `--agent-impl` on run/batch; resume inherits stored `agent_impl` and refuses mismatch, not bypassable by `--force-resume`. | Prevents meaningless mixed-implementation resume. |
| Comparability | Langgraph excluded from headline aggregates by default; score/report group by `agent_impl`; explicit opt-in only. | Preserves benchmark integrity. |

## 16. Recommendation

Implement **Option C** with **parity LLM adapter**, **registry-schema passthrough**, **SQLite-authoritative checkpoint/resume**, and **no changes to gate/scoring/safety**. Treat LangGraph as an experimental orchestration backend behind `agent_impl`, not as the harness.

The key learning outcome should be a measured diff: what LangGraph removes from `harness/loop.py` versus what must remain hand-written for safety, provenance, and crash semantics. If parity mode proves LangGraph adds little, that is a useful negative result; the sandbox crash-lab and native-LC experiments provide the deeper framework learning without risking benchmark credibility.