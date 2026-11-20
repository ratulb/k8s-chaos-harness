# PHASES.md — etcd-chaos-bench

Status legend: `[ ]` not started, `[~]` in progress, `[x]` done, `[!]` blocked

## Phase 0 — Foundations
- [~] Kaggle LLM bootstrap script
- [~] Ollama health check
- [ ] zrok tunnel automation
- [ ] Hosted backend smoke tests
- Deliverable: one-command LLM readiness check
- Acceptance: local wrapper can reach Kaggle and one hosted backend

## Phase 1 — Substrate and reset
- [ ] 3-control-plane cluster setup
- [ ] `reset/rebuild_cluster.sh`
- [ ] `verify/etcd_healthy_3_members.sh`
- [ ] Baseline check before injection
- Deliverable: repeatable healthy cluster
- Acceptance: reset succeeds 3 times in a row

## Phase 2 — Fault injection
- [ ] `faults/etcd_member_down.sh`
- [ ] `faults/etcd_member_wiped.sh`
- [ ] `faults/etcd_quorum_loss.sh`
- [ ] `faults/etcd_disk_full.sh`
- [ ] `faults/etcd_wrong_peer_url.sh`
- [ ] `faults/control_healthy_noisy.sh`
- [ ] Determinism check for each
- Deliverable: deterministic inject + verify
- Acceptance: same symptom on two runs

## Phase 3 — Diagnostic tool layer
- [ ] Read-only tool implementations
- [ ] Output truncation
- [ ] Trace recorder
- [ ] Budget enforcer
- Deliverable: model can investigate freely
- Acceptance: no mutations, traces written

## Phase 4 — Diagnosis and gate
- [ ] `submit_diagnosis`
- [ ] Gated repair tools
- [ ] Forbidden reasons
- [ ] Approval token flow
- Deliverable: diagnosis required before repair
- Acceptance: forbidden repairs blocked and logged

## Phase 5 — Full loop
- [ ] One scenario end to end
- [ ] Wrong-repair trial
- [ ] Recovery verification
- Deliverable: complete benchmark loop
- Acceptance: score record written

## Phase 6 — Checkpointer
- [ ] SQLite schema
- [ ] Save/resume state
- [ ] Crash recovery test
- Deliverable: mid-run resume
- Acceptance: killed session resumes

## Phase 7 — Sweep
- [ ] 5 scenarios x 2 backends x N runs
- [ ] Score aggregation
- [ ] Comparison table
- Deliverable: cross-backend results
- Acceptance: rates, not anecdotes

## Phase 8 — Publish
- [ ] README
- [ ] Blog post
- [ ] Demo notes
- Deliverable: publishable artifact
- Acceptance: reproducible from clean machine
