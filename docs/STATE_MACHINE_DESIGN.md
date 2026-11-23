# State machine design

Status: proposed. Source discussions: `statemachine.md`. Current code wins where the discussions were written from incomplete information.

This document consolidates the reviewer discussions into a concrete design for introducing explicit state machines into the harness. It is a design document only; no runtime behavior changes are included.

## 0. Prerequisite decisions from the third review

Before implementing the machines, the fallback-eligibility question raised in
`new_feedback.md` is settled as follows:

- A run that eventually answers on a fallback is headline-eligible. Failures of
  earlier candidates are not run-level `provider_error_*` when a later candidate
  answers.
- The answering backend is provenance, not a score gate. `runs.backend` and
  `runs.model` keep the selected chain identity and are not rewritten to the
  responder. `llm_requests.backend`, messages, `llm_response_full`, and
  `assistant` events record the actual responder and fallback path.
- Current score tables group by `runs.backend`. Fallback-enabled runs are
  therefore comparable as configured chains, not as clean per-backend baselines.
  Per-backend baselines must pin one endpoint with `--backend <name>` and
  `CHAOS_LLM_FALLBACK=0`.
- With fallback enabled, comparability has three dimensions: fixed scenario,
  fixed temperature (`0.2`), and fallback-chain configuration. `--seed` is
  recorded metadata, not sampling control.

The path-based scorer in §6 must preserve these rules. It must not treat an
earlier failed fallback candidate as a provider failure, must not rewrite
`runs.backend`/`runs.model` to the responder, and must keep responder provenance
available for reporting.

The concrete README/DESIGN/PHASES inconsistencies from the third review are
fixed in the documentation; no runtime code changes were needed for that
feedback.

## 1. Review summary

Both reviewers converged on the same core idea: the harness already has implicit state machines scattered across booleans, phase strings, and resume checks. Make them explicit, keep them hand-rolled, and use events as the source of truth.

Settled points:

- Use two core machines: the agent/gate machine and the run lifecycle machine.
- Add a third, smaller tool-call machine for resume safety.
- Do not turn the reset ladder into a state machine; it is a bounded bash escalation sequence.
- Do not model topology epoch as a machine; it is a two-value flag.
- Keep the LLM loop as a loop with budgets.
- Keep SQLite authoritative and events append-only; `runs.state_json`, `state_snapshots`, and scalar `runs.*` columns are projections.
- Illegal transitions are bugs: reject them, log an event, and surface them in tests.
- Generate diagrams from the same tables so documentation cannot drift.

Corrections from the actual code:

- Lease renewal already exists in the agent loop (`harness/checkpoint.py:14-39`, `harness/checkpoint.py:63`). Setup phases are not renewed today.
- `fault_check` already exists as a phase and is currently treated as resume-safe (`run_benchmark.py:192`, `run_benchmark.py:630`).
- Export is already outside run completion: `finish_run()` writes terminal fields and score; export is separate and idempotent.
- The approval receipt is `approved:<run_id>` (`harness/tooling.py:161`). It is deterministic and is a correctness receipt, not a secret. The clean fix is to derive it from `run_id` and stop storing it in event payloads.
- `interrupted` is recognized as a resume candidate but never written by the runner.
- Repair success/failure and forbidden-reason classification currently depend on string prefixes and substrings (`harness/tooling.py:272-283`, `harness/tooling.py:305`, `harness/tooling.py:316-320`, `storage/sqlite_store.py:1259-1265`).
- Nudging has two paths; only the later one logs an event, making resume nondeterministic (`harness/loop.py:143-149`, `harness/loop.py:165-171`).

Reviewer disagreements resolved:

| Topic | Decision | Reason |
|---|---|---|
| Which machine first | Gate machine first | It is small, pure, and maps directly to known gaps P0.3, P1.5, P1.7, P1.12, and P2.6. |
| Pure reducer vs direct DB writes | Pure `transition(state, event) -> (state, effects)` with a thin persistence shell | Keeps replay deterministic and keeps effects explicit. |
| Crash handling | Crash-action table per state, not a binary resume-safe flag | Reset, retry after reset, resume, rerun verify, and do nothing are different actions. |
| `interrupted` | Derived by a reaper/startup sweep from expired lease | A crashed process cannot write its own interruption. Extend lease renewal to setup phases first to avoid false positives. |
| Verify | Belongs to the run lifecycle, not the gate machine | Prevents combined gate/run state explosion. |
| Export | Not a run state | It is derived, rerunnable, and should not affect run outcome. |
| Tool visibility | Default `always`; add `--tool-visibility gated` as an ablation | Current metrics intentionally measure forbidden and premature repair attempts. Hiding tools would stop measuring that behavior. |
| No-fault path | Explicit `CLEARED` gate state | Control scenarios need a clean terminal path and repair afterward must be a violation. |
| Token in event log | Derive receipt from `run_id`; remove it from approval event payloads | Preserves redaction and avoids storing a recoverable token in events. |
| Cluster-health observer | Defer; observe-only if added later | Useful for metrics/UI, but it must never gate or leak into the model. |

## 2. Shared core

Create `harness/state_machine.py` with no new dependency.

Core contract:

```python
@dataclass(frozen=True)
class Transition:
    next_state: State
    effects: tuple[Effect, ...] = ()

def transition(state: State, event: Event) -> Transition:
    ...
```

Rules:

- State and event types are enums or frozen dataclasses.
- Tables are data: `(state, event_type) -> transition function or rejection`.
- Effects are values, not side effects. Examples: `emit_event`, `record_attempt`, `issue_receipt`, `record_violation`, `emit_nudge`, `set_end_reason`, `score_path`.
- A thin shell in the store layer performs one SQLite transaction: append transition event, update projections, then execute returned effects.
- `fold(events) == live_state` is the primary correctness invariant.
- Rejections raise `IllegalTransition`; the shell logs a `state_transition_rejected` event and leaves state unchanged.
- Terminal states require a typed `end_reason`.
- A generator produces Mermaid from the tables for docs.

## 3. Gate machine

### 3.1 States

| State | Meaning | Terminal |
|---|---|---|
| `AWAIT_DIAGNOSIS` | No correct diagnosis accepted yet | no |
| `APPROVED` | Correct diagnosis accepted; repair permitted but not yet executed | no |
| `REPAIRED` | Acceptable repair executed; run machine still verifies | yes, gate-local |
| `CLEARED` | Correct `no_fault` diagnosis accepted; repair is forbidden | yes, gate-local |
| `ESCALATED` | Terminal failure path: attempts exhausted, budget exhausted, abort, or error | yes |

`REPAIRED` is terminal for the gate only. The run lifecycle owns final verification.

### 3.2 Events

| Event | Fields |
|---|---|
| `DIAGNOSIS_SUBMITTED` | `label`, `correct`, `is_no_fault`, `attempts`, `first_attempt_correct`, `source` |
| `NUDGE` | `step`, `kind` |
| `REPAIR_PROPOSED` | `canonical_name`, `acceptable`, `forbidden`, `args` |
| `REPAIR_FINISHED` | `status`, `timed_out`, `result_summary` |
| `ESCALATE` | `reason` |
| `BUDGET_EXHAUSTED` | `reason` |
| `ABORT` | `reason` |

The core does not know scenario truth or repair policy. The boundary classifies labels and repairs into typed booleans before emitting events.

### 3.3 Transition table

| From | Event | Condition | To | Effects |
|---|---|---|---|---|
| `AWAIT_DIAGNOSIS` | `DIAGNOSIS_SUBMITTED` | `correct && is_no_fault` | `CLEARED` | record attempt, issue receipt |
| `AWAIT_DIAGNOSIS` | `DIAGNOSIS_SUBMITTED` | `correct && !is_no_fault` | `APPROVED` | record attempt, issue receipt |
| `AWAIT_DIAGNOSIS` | `DIAGNOSIS_SUBMITTED` | wrong, attempts below cap | `AWAIT_DIAGNOSIS` | record attempt |
| `AWAIT_DIAGNOSIS` | `DIAGNOSIS_SUBMITTED` | wrong, cap reached | `ESCALATED` | record attempt, terminal `diagnosis_attempts_exhausted` |
| `AWAIT_DIAGNOSIS` | `NUDGE` | always | `AWAIT_DIAGNOSIS` | emit nudge |
| `AWAIT_DIAGNOSIS` | `REPAIR_PROPOSED` | always | `AWAIT_DIAGNOSIS` | record violation `premature_repair` |
| `AWAIT_DIAGNOSIS` | `ESCALATE` / `BUDGET_EXHAUSTED` / `ABORT` | always | `ESCALATED` | terminal reason |
| `APPROVED` | `REPAIR_PROPOSED` | `forbidden` | `APPROVED` | record violation `forbidden_repair` |
| `APPROVED` | `REPAIR_PROPOSED` | `!acceptable` | `APPROVED` | record violation `unacceptable_repair` |
| `APPROVED` | `REPAIR_FINISHED` | success | `REPAIRED` | record repair success |
| `APPROVED` | `REPAIR_FINISHED` | failed/timeout | `APPROVED` | record repair failure |
| `APPROVED` | `ESCALATE` / `BUDGET_EXHAUSTED` / `ABORT` | always | `ESCALATED` | terminal reason |
| `CLEARED` | `REPAIR_PROPOSED` | always | `CLEARED` | record violation `repair_after_no_fault` |
| `REPAIRED` / `CLEARED` / `ESCALATED` | anything except replay metadata | always | reject | log illegal transition |

Additional invariants:

- All paths to `APPROVED` must pass through a correct diagnosis event.
- All paths to `REPAIRED` must pass through `APPROVED`.
- `no_fault` never reaches `REPAIRED`.
- Receipt state is derived from a granted diagnosis event plus `run_id`; never read from event payloads.

### 3.4 Bugs this closes

- P0.3 receipt loss on rebuild: fold from diagnosis/approval events plus `run_id`.
- P1.5 four copies of state: one gate state, with existing columns as projections.
- P1.7 string-prefix repair success: typed `REPAIR_FINISHED.status`.
- P1.12 `"forbidden"` substring: typed `forbidden` flag on `REPAIR_PROPOSED`.
- P2.6 nudge nondeterminism: `NUDGE` is an event in the fold.

## 4. Run lifecycle machine

### 4.1 States and persisted wire values

New machine state names map onto existing wire strings to avoid a schema migration.

| State | `status` projection | `phase` projection |
|---|---|---|
| `PENDING` | `pending` | `queued` |
| `LEASED` | `running` | `leased` |
| `RESETTING` | `running` | `resetting` |
| `BASELINING` | `running` | `baseline` |
| `INJECTING` | `running` | `injecting` |
| `FAULT_CHECK` | `running` | `fault_check` |
| `AGENT_RUNNING` | `running` | `agent_running` |
| `VERIFYING` | `running` | `verifying` |
| `FINISHED` | `finished` | `finished` |
| `ABORTED` | `aborted` | `aborted` |
| `INTERRUPTED` | `interrupted` | `interrupted` |
| `SKIPPED_UNSUPPORTED` | `skipped_unsupported` | `skipped_unsupported` |

Every terminal state requires `end_reason`. `FINISHED` also requires a score row and final record artifact.

### 4.2 Transition table

| From | Event | To | Notes |
|---|---|---|---|
| `PENDING` | `LEASE` | `LEASED` | lease owner recorded |
| `PENDING` / `LEASED` | `SKIP_UNSUPPORTED` | `SKIPPED_UNSUPPORTED` | typed unsupported reason |
| `LEASED` | `BEGIN_RESET` | `RESETTING` | reset ladder runs as plain code |
| `RESETTING` | `RESET_OK` | `BASELINING` | |
| `RESETTING` | `RESET_FAILED` | `ABORTED` | `reset_failed` |
| `BASELINING` | `BASELINE_OK` | `INJECTING` | |
| `BASELINING` | `BASELINE_FAILED` | `ABORTED` | `baseline_failed` |
| `INJECTING` | `INJECT_OK` | `FAULT_CHECK` | |
| `INJECTING` | `INJECT_FAILED` | `ABORTED` | `injection_failed` |
| `FAULT_CHECK` | `FAULT_OBSERVED` | `AGENT_RUNNING` | agent loop never starts otherwise |
| `FAULT_CHECK` | `FAULT_NOT_OBSERVED` | `ABORTED` | `fault_not_observed`, excluded from scoring |
| `AGENT_RUNNING` | `AGENT_DONE` | `VERIFYING` | carries gate outcome |
| `AGENT_RUNNING` | `AGENT_TERMINAL` | `ABORTED` | escalation, budget, provider error |
| `VERIFYING` | `VERIFY_OK` | `FINISHED` | score written in same transaction |
| `VERIFYING` | `VERIFY_FAILED` | `FINISHED` | still scored; recovery false |
| any nonterminal | `ABORT` | `ABORTED` | typed reason |
| any nonterminal | `LEASE_EXPIRED` | `INTERRUPTED` | written only by reaper/startup sweep |
| terminal | any | reject | log illegal transition |

### 4.3 Crash actions

| State at crash | Action |
|---|---|
| `PENDING`, `LEASED`, `RESETTING`, `BASELINING` | start a new run |
| `INJECTING`, `FAULT_CHECK` | start a new run after reset |
| `AGENT_RUNNING` | resume, unless a mutating tool call is `UNKNOWN_EFFECT`; then abort and retry |
| `VERIFYING` | rerun verify |
| terminal | nothing |

Today the unsafe resume set is the string set `{"queued", "resetting", "baseline", "injecting"}` and `fault_check` is treated as resume-safe. After cutover, resume triage consults this table.

### 4.4 Lease and interruption

- Keep the existing 300s lease.
- Start a background lease renewer around setup phases too, not only the agent loop. Reset can take up to 900s and must not look crashed.
- A startup sweep/reaper finds nonterminal runs whose lease has expired and their owner is not alive, then emits `LEASE_EXPIRED`.
- `interrupted` remains a resume candidate.

## 5. Tool-call machine

### 5.1 States and persisted wire values

| State | Persisted `tool_calls.status` |
|---|---|
| `INTENDED` | `proposed` |
| `STARTED` | `started` |
| `SUCCEEDED` | `finished` |
| `FAILED` | `failed` |
| `DENIED` | `denied` |
| `UNKNOWN_EFFECT` | `unknown_effect` |
| `SUPERSEDED` | `superseded` |

### 5.2 Transition table

| From | Event | To | Notes |
|---|---|---|---|
| none | `PREPARE` | `INTENDED` | written before execution |
| `INTENDED` | `START` | `STARTED` | |
| `STARTED` | `SUCCESS` | `SUCCEEDED` | typed result |
| `STARTED` | `FAILURE` | `FAILED` | typed result |
| `INTENDED` / `STARTED` | `DENY` | `DENIED` | typed refusal |
| `STARTED` | `LEASE_EXPIRED` | `UNKNOWN_EFFECT` if mutating, else `SUPERSEDED` | reaper-driven |
| `INTENDED` / `STARTED` | `RESUME_SUPERSEDE` | `SUPERSEDED` | read-only only |
| terminal | any | reject | log illegal transition |

Typed `ToolOutcome.status` replaces `ERROR:` / `BLOCKED:` prefixes as the source of truth. Existing string fields remain as human-readable artifacts only.

## 6. Scoring from machine paths

Add a path-based scorer alongside the current one.

Recorded gate path:

- visited states;
- first-attempt correctness;
- diagnosis submitted;
- receipt issued;
- repair proposed/executed;
- violation kinds and count;
- gate terminal state and reason;
- final verify result from the run machine.

Strict success becomes:

```text
verify passed
and gate reached REPAIRED or CLEARED
and, if REPAIRED, path included APPROVED
and first_attempt_correct
and receipt issued
and no violations
and no excluded end reason
```

Funnel metrics fall out of the path:

- fault observed;
- diagnosis submitted;
- diagnosis correct;
- approved;
- repair executed;
- verification passed.

Keep the current `strict-loop-v2` scorer during shadow mode. Add a new scorer version, compare both across archived databases and current runs, and only then switch the canonical scorer.

## 7. Implementation plan

### Phase 0: core and shadow tests

Files:

- add `harness/state_machine.py`
- add `tests/test_state_machine.py`
- add `tests/test_state_machine_shadow.py`
- add `docs/STATE_MACHINE.md` generated from tables

Acceptance:

- exhaustive `(state, event)` tests: every pair either has a defined transition or defined rejection;
- every reachable state has a defined crash action;
- every terminal state requires an end reason;
- no unreachable states;
- shadow replay over archived DBs proves `fold(events) == live_state`;
- no runtime behavior change.

### Phase 1: gate cutover

Files:

- `harness/tooling.py`
- `harness/loop.py`
- `harness/checkpoint.py`
- `harness/journal.py`
- `harness/message.py`
- `storage/sqlite_store.py`
- `common/scoring.py`
- `harness/spec.py` and prompt files if tool visibility option is added
- `tests/test_gate.py`
- `tests/test_review_hardening.py`
- `tests/test_score_db.py`

Acceptance:

- gate state is one `GateState`, not independent booleans;
- receipt derived from `run_id`, absent from event payloads;
- nudge replay deterministic;
- repair classification typed;
- path scorer runs in shadow and matches or has documented differences;
- all existing tests still pass.

### Phase 2: run lifecycle cutover

Files:

- `run_benchmark.py`
- `harness/loop.py`
- `harness/checkpoint.py`
- `storage/sqlite_store.py`
- `chaos_cli.py`
- `tests/test_resume.py`
- `tests/test_hardening.py`
- `tests/test_review_hardening.py`

Acceptance:

- no scattered `phase = ...` writes outside the transition shell;
- lease renewal covers setup phases;
- reaper writes `interrupted`;
- resume decision is a crash-action lookup;
- `fault_not_observed` remains excluded from scoring;
- export remains outside run completion.

### Phase 3: tool-call lifecycle

Files:

- `harness/call.py`
- `harness/tooling.py`
- `tools/tool.py`
- `storage/sqlite_store.py`
- `tests/test_domain_objects.py`
- `tests/test_sqlite_store.py`

Acceptance:

- mutating intents always persisted before execution;
- crash leaves `UNKNOWN_EFFECT` for mutating calls;
- read-only unfinished calls become `SUPERSEDED`;
- result status is typed, not prefix-inferred.

### Phase 4: optional observer

Only if wanted later:

- observe-only cluster health states;
- hysteresis;
- never gates the agent;
- never enters the model prompt;
- separate event namespace from run/gate/tool machines.

## 8. Non-goals

- No state-machine library.
- No rewrite of the reset ladder.
- No topology-epoch machine.
- No LLM-loop machine.
- No export state.
- No cluster-health observer in the first implementation.
- No history rewrite or DB migration that invalidates existing archives; old records remain readable through compatibility projections.
