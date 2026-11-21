# PHASES.md — k8s-chaos-harness

Status legend: `[ ]` not started, `[~]` in progress, `[x]` done, `[!]` blocked

Canonical design: `DESIGN.md`. Historical inputs: `docs/archive/SQLite.md`,
`docs/archive/feedback.md`.

Cluster/GPU rebuild is in progress; wait for operator confirmation before live
or destructive validation.

## Skipped or deferred live work
- `vm1` is back; live topology discovery and `chaos cluster nuke` rebuild
  acceptance passed on the three-member cluster.
- Live backend smoke (`CHAOS_LIVE_BACKEND_TESTS=1`) passed from this workspace.
- Reduced live sweep completed: 13 runs across the four backends and four
  scenarios. Destructive expansion was stopped by operator decision; no
  authoritative `/data/k8s-chaos-harness` database has been touched.
- The operator started a manual `--ui live` run from `vm2`; that run is owned by the
  operator and was not inspected, aborted, or modified from this session.
- Prompt approval is covered offline only; no live interactive repair run has been
  attempted.
- Static replay was generated from the final sweep database and saved under
  `/tmp/opencode/final-replay.html` with score reports alongside it.

## Phase 0 — Foundations
- [x] Ollama-compatible bootstrap script
- [x] Ollama health check
- [x] zrok tunnel workflow documented as optional self-hosted transport
- [x] Hosted backend smoke tests
- Deliverable: one-command LLM readiness check
- Acceptance: local wrapper can reach a self-hosted endpoint and one hosted backend

## Phase 1 — Substrate and reset
- [x] Initial 3-control-plane cluster setup
- [x] `cluster/reset_cluster.sh`
- [x] `cluster/verify_etcd_healthy.sh`
- [x] Baseline check before injection
- [x] Generalize verification away from hardcoded member count
- Deliverable: repeatable healthy cluster on arbitrary N topology
- Acceptance: reset/verify works on 1, 2, 3, 4, and 5+ member fixtures or clusters

## Phase 2 — Fault injection
- [x] `faults/etcd_member_down.sh`
- [x] `faults/etcd_member_wiped.sh`
- [x] `faults/etcd_quorum_loss.sh`
- [x] `faults/etcd_disk_full.sh`
- [x] `faults/etcd_wrong_peer_url.sh`
- [x] `faults/control_healthy_noisy.sh`
- [~] Determinism check for each; all six inject successfully, repeatability varies
- [x] Dynamic target selectors for arbitrary control-plane counts
- [x] Dynamic quorum-loss target calculation
- Deliverable: deterministic inject + verify on arbitrary topology
- Acceptance: same observable symptom twice on a reference 3-member topology

## Phase 3 — Diagnostic tool layer
- [x] Read-only tool implementations
- [x] Output truncation
- [x] Trace recorder
- [x] Budget enforcer
- [x] Node enums from frozen dynamic topology
- Deliverable: model can investigate freely without fixed node assumptions
- Acceptance: no mutation, traces written, node enums match live topology

## Phase 4 — Diagnosis and gate
- [x] `submit_diagnosis`
- [x] Gated repair tools
- [x] Forbidden reasons
- [x] Approval token flow
- [x] Gate unit tests (`tests/test_gate.py`)
- Deliverable: diagnosis required before repair
- Acceptance: forbidden repairs blocked and logged

## Phase 5 — Full loop
- [x] One scenario end to end (`etcd-member-down`, Groq seed 101; Kaggle seed 403)
- [x] Wrong-repair trial, unit-tested gate
- [x] Recovery verification
- Deliverable: complete benchmark loop
- Acceptance: score record written

## Phase 6 — Checkpointer
- [x] SQLite schema with append-only event log + state cache
- [x] Tool intent/result write-ahead lifecycle
- [x] Scenario spec versioning and resume lineage
- [x] Save/resume state
- [x] Legacy JSONL import and derived exports
- [x] Crash recovery test coverage, offline unit tests
- [x] One-command wrapper and DB CLI
- Deliverable: mid-run resume
- Acceptance: killed session resumes; unknown repair aborts and retries fresh

## Phase 7 — Sweep
- [~] 6 scenarios x 1 backend x multiple runs; partial historical sweep exists
- [x] Score aggregation (`score.py`, now with `--db`)
- [x] Comparison table from historical sweeps
- [ ] Fresh cross-backend sweep after Phase 9 hardening
- [ ] Candidate extra scenario: rebuild with prior etcd state; consider
  https://github.com/ratulb/kubemcp as fallback provisioning path; it copies/uses
  https://github.com/ratulb/k8s-easy-install, worked on GCP, and may be broken
  on Azure
- Deliverable: current cross-backend results
- Acceptance: rates, not anecdotes, from the active post-redesign epoch
- Reduced cross-backend sweep completed: 13 runs, diagnosis 0.538, verify 0.923,
  ollama diagnosis 1.0; destructive expansion stopped by operator decision

## Phase 8 — Publish
- [x] Initial README
- [x] Updated README as the main guide for dynamic topology, `ollama`, and `./chaos`
- [ ] Blog post
- [ ] Demo notes
- Deliverable: publishable artifact
- Acceptance: reproducible from clean machine

## Phase 9 — Solidify foundation and operational UX

### Phase 9A — Documentation baseline
- [x] Rewrite `DESIGN.md` as the reconciled source of truth
- [x] Add locked decisions, dynamic topology, modes, SQLite v3, CLI, live view,
      rebuild flow, scoring, and done criteria
- [x] Rewrite `PHASES.md` with honest history and actionable phases
- [x] Update `README.md` quickstart
- [x] Update `NOTES.md` canonical-document pointers
- [x] Update `kaggle/README.md` benchmark example to use `ollama`
- Deliverable: no ambiguity about what is being built
- Acceptance: design matches every locked decision; no stale prescriptive
  `kaggle` backend or required-three-node language remains

### Phase 9B — Backend naming and scenario metadata
- [x] Rename public backend `kaggle` → `ollama`
- [x] Normalize stored/imported backend value `kaggle` to `ollama`
- [x] Update `.env.example`, scripts, comments, examples, and docs
- [x] Keep historical reviewer/feedback files unchanged
- [x] Add scenario fields: `risk`, menu summary, `min_etcd_members`, selector
      requirements, expected `impact_class`
- [x] Implement `chaos scenario list|show|validate`
- [x] Generate `menu.sh` from `chaos scenario list --json`
- Deliverable: clear backend identity and self-describing scenarios
- Acceptance: `ollama` is the only user-facing self-hosted backend name; invalid
  scenarios fail validation before destructive action

### Phase 9C — Dynamic topology and capability checks
- [x] Implement `tools/topology.py`
- [x] Discover nodes, control planes, etcd members, member IDs, peer URLs, probe
      node, topology source, and confidence
- [x] Compute `quorum = floor(N / 2) + 1`
- [x] Compute `quorum_losing_set = N - floor(N / 2)`
- [x] Resolve numeric, named, alias, and quorum selectors against frozen topology
- [x] Implement capability result: `eligible`, `warning`, `unsupported_reason`,
      `impact_class`
- [x] Remove hardcoded 3-node assumptions from `tools/cluster_config.py`,
      `cluster/config.sh`, verify scripts, quorum fault, and agent prompt
- [x] Wire topology preflight, frozen topology artifacts, and capability events
      into `run_benchmark.py`
- [x] Add `chaos target show`
- Deliverable: arbitrary N topology is understood before any run
- Acceptance: offline fixtures cover N=1, 2, 3, 4, 5, 7; quorum targets are
  correct; N=1 quorum-loss is skipped in benchmark mode

### Phase 9D — SQLite schema v3
- [x] Add additive v3 migration
- [x] Add `batches.topology_hash`
- [x] Add `runs.topology_hash`, `runs.interactive`, `runs.launch_id`, `runs.pid`
- [x] Add `state_snapshots`
- [x] Add `ui_stream`
- [x] Add store-level `RLock` for background UI/probe readers
- [x] Update `doctor --strict`, `status`, export, and migration tests
- Deliverable: SQLite can record topology, snapshots, and UI stream safely
- Acceptance: v2 DB migrates to v3; `doctor --strict` passes on a temp DB

### Phase 9E — State lifecycle
- [x] Implement `chaos state discard [--yes]`
- [x] Delete active DB/WAL/SHM, truncate `runs.jsonl`, delete generated traces,
      clear IP cache, reinitialize empty DB
- [x] Implement normal-run topology hash mismatch auto-discard
- [x] Refuse explicit `resume` on topology mismatch
- [x] Make `status`, `attach`, and `show` read-only with stale warnings
- [x] Abort active runs before discard; refuse if abort fails
- [x] Remove automatic `import-legacy` from `run_benchmark.sh`
- Deliverable: stale epochs never contaminate scoring or resume
- Acceptance: temp-DB tests prove auto-discard, resume refusal, and no discard
  from read-only commands

### Phase 9F — Rebuild flow
- [x] Implement `chaos cluster nuke [--yes]`
- [x] Validate `setup.conf` and SSH preflight listed masters
- [x] Rebuild through `k8s-easy-install`
- [x] Discover post-rebuild live topology
- [x] Warn on `setup.conf` vs live topology difference and accept live topology
- [x] Refresh node/IP cache from live cluster
- [x] Discard stale state only after successful rebuild
- [x] Preserve SQLite and report clearly on failed rebuild
- Deliverable: one-command rebuild with clean operational state
- Acceptance: failure keeps old DB; success produces fresh DB and live topology
  report
- Live acceptance passed on 2026-11-21; all three nodes returned Ready and the
  fresh DB reported zero runs

### Phase 9G — Streaming and live terminal view
- [x] Add `on_chunk` streaming to `LLMClient` with non-stream fallback
- [x] Write redacted stream chunks to `ui_stream`
- [x] Implement SQLite-backed reporter/state snapshots
- [x] Build TTY live renderer with plain fallback (no new dependency)
- [x] Show scenario, backend, mode, run id, topology, elapsed/phase timers,
      steps/tokens, stall warning, streaming text, tool tail, cluster strip,
      event log
- [x] Print final JSON only after stopping Live
- [x] Send child stdout/stderr to `logs/<run_id>.log`
- [x] Update `score.py` exclusions: resumed, interactive, pre-gate, unsupported
- Deliverable: operator can see what the harness and agent are doing
- Acceptance: stale-free UI on arbitrary node counts; final JSON parseable

### Phase 9H — Detached run, attach, and abort
- [x] Implement `chaos run --detach`
- [x] Record `launch_id` and `pid` on runs
- [x] Implement `chaos attach <run_id>` with `--follow` and separate event/UI cursors
- [x] Implement `chaos abort <run_id|all>` and abort-all on rebuild/discard
- [x] Ensure `attach` is read-only
- Deliverable: long runs can be started and observed safely
- Acceptance: attach follows the same SQLite events; abort records terminal
  status

### Phase 9I — Prompt approval
- [x] Add optional `--approve prompt`
- [x] Default remains `--approve policy`
- [x] Stop Live before prompting
- [x] Never display approval token
- [x] Mark prompt-approval runs `interactive=1`
- [x] Exclude interactive runs from headline score by default
- Deliverable: optional human-in-the-loop without corrupting benchmark rates
- Acceptance: prompt runs appear in DB but not headline rates unless requested

### Phase 9J — Tests and hardening
- [x] Unit tests for topology discovery parsers and confidence labels
- [x] Unit tests for quorum math and capability checks
- [x] Unit tests for backend rename and scenario validation
- [x] Unit tests for SQLite v3 migration and doctor
- [x] Unit tests for auto-discard and resume refusal
- [x] Unit tests for exporter/score exclusions
- [x] Unit tests for live renderer with fake SQLite events
- [x] Unit tests for detach/attach/abort and prompt approval
- [x] Run full offline suite, compileall, `bash -n`, `git diff --check`
- Deliverable: solid foundation before live sweeps
- Acceptance: all offline tests pass with machines down

## Phase 10 — Static replay and results matrix
- [x] Static HTML replay from SQLite events
- [x] Results matrix by backend, scenario, topology class, and seed
- [x] Redaction review before publishing artifacts
- Deliverable: publishable visual artifact
- Acceptance: replay renders arbitrary topology snapshots without re-running
  the cluster

## Phase 11 — Credibility hardening before publication
- [x] Add bounded fault-present checks before every injected agent run
- [x] Fail closed as `fault_not_observed` when no stimulus is observed
- [x] Fix member-down success reporting and live etcd peer-URL injection
- [x] Add strict metric and exclude provider/observer failures from headline rates
- [x] Classify provider 413/429/5xx end reasons
- [x] Add default-deny destructive guard for live benchmark runs
- [x] Route generated menus through `./chaos run`
- [x] Request and consume streamed usage payloads
- [x] Archive historical reviewer documents
- [x] Run full offline suite after changes
- [ ] Calibrate fault-present timeouts on the rebuilt cluster
- [ ] Run non-destructive live smoke after cluster/GPU are ready
- [ ] Run destructive calibration and model pilots only after explicit approval
- [ ] Run full public sweep and publish refreshed replay/score artifacts
- Deliverable: benchmark results that prove the faults existed and separate
  model failures from harness/provider failures
- Acceptance: every scored run has an observed fault, no provider failure, and
  a strict metric backed by auditable fault-check artifacts
