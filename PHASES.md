# PHASES.md — k8s-chaos-harness

Status legend: `[ ]` not started, `[~]` in progress, `[x]` done, `[!]` blocked

## Phase 0 — Foundations
- [x] Kaggle LLM bootstrap script
- [x] Ollama health check
- [x] zrok tunnel workflow documented (Kaggle bootstrap + local port-forward)
- [x] Hosted backend smoke tests
- Deliverable: one-command LLM readiness check
- Acceptance: local wrapper can reach Kaggle and one hosted backend

## Phase 1 — Substrate and reset
- [x] 3-control-plane cluster setup
- [x] `cluster/reset_cluster.sh`
- [x] `cluster/verify_etcd_healthy.sh`
- [x] Baseline check before injection
- Deliverable: repeatable healthy cluster
- Acceptance: reset succeeds 3 times in a row (done twice so far)

## Phase 2 — Fault injection
- [x] `faults/etcd_member_down.sh`
- [x] `faults/etcd_member_wiped.sh`
- [x] `faults/etcd_quorum_loss.sh`
- [x] `faults/etcd_disk_full.sh`
- [x] `faults/etcd_wrong_peer_url.sh`
- [x] `faults/control_healthy_noisy.sh`
- [~] Determinism check for each (all six inject successfully; repeatability varies)
- Deliverable: deterministic inject + verify
- Acceptance: same symptom on two runs

## Phase 3 — Diagnostic tool layer
- [x] Read-only tool implementations
- [x] Output truncation
- [x] Trace recorder
- [x] Budget enforcer
- Deliverable: model can investigate freely
- Acceptance: no mutations, traces written

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
- [x] Wrong-repair trial (unit-tested gate)
- [x] Recovery verification
- Deliverable: complete benchmark loop
- Acceptance: score record written

## Phase 6 — Checkpointer
- [x] SQLite schema (append-only event log + state cache)
- [x] Tool intent/result write-ahead lifecycle
- [x] Scenario spec versioning and resume lineage
- [x] Save/resume state
- [x] Legacy JSONL import and derived exports
- [x] Crash recovery test coverage (offline unit tests)
- [x] One-command wrapper and DB CLI
- Deliverable: mid-run resume
- Acceptance: killed session resumes; unknown repair aborts and retries fresh

## Phase 7 — Sweep
- [~] 6 scenarios x 1 backend x multiple runs (Kaggle); Groq smoke only
- [x] Score aggregation (`score.py`, now with `--db`)
- [x] Comparison table from Kaggle sweeps (`python3 score.py --db results.sqlite3`)
- Deliverable: cross-backend results
- Acceptance: rates, not anecdotes

## Phase 8 — Publish
- [x] README
- [ ] Blog post
- [ ] Demo notes
- Deliverable: publishable artifact
- Acceptance: reproducible from clean machine
