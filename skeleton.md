# Skeleton roadmap — K8s fault-healing agent

> Historical roadmap. Superseded by `DESIGN.md`; Docker Compose/Multipass and
> MCP-first options below are no longer the selected substrate/transport.

## Vision
An agent that observes a Kubernetes cluster, diagnoses injected faults, and
proposes/executes fixes via tools, using an LLM (self-hosted small model or
free endpoints) as the reasoning engine.

## Building blocks already available
- Cluster provisioning: k8s-easy-install (multi-node, Calico VXLAN).
- Disaster/etcd drills: kube-etcd-switch (snapshots, external etcd).
- LLM tool loop: tool_call_loop (Ollama + Groq/OpenRouter/Cloudflare).
- Kaggle GPU tunneling: zrok SSH tunnel.

## Architecture (high level)
- LLM client abstraction: one interface, many backends.
- Agent loop: observe -> reason -> tool call -> execute -> verify -> repeat.
- Tool layer: kubectl, etcd snapshot/restore, node ops, network probes.
- Guardrails: allow-list, dry-run, approvals, rollback.
- State builder: compress cluster state/logs/events into LLM context.
- Fault injector: reproducible faults for testing.
- Evaluator: inject -> heal -> measure.

## Phase 0 — Foundations
- Keep cluster healthy; document baseline.
- Verify LLM backends (local + free endpoints) with tool calls.
- Smooth Kaggle tunnel workflow.

## Phase 1 — Minimal agent MVP
- One fault (e.g., delete a pod / scale deployment to 0).
- One tool (kubectl get pods) + answer.
- Reuse tool_call_loop pattern; local 3B model.

## Phase 2 — Toolset + safety
- Add describe, logs, events, apply, delete, rollout, cordon/drain.
- Allow-list + argument validation; dry-run flag; human approval gate.

## Phase 3 — Fault injection
- Pod kill, bad image, node taint, network block (iptables), etcd snapshot corruption.
- Each injector returns expected symptom + recovery oracle.

## Phase 4 — State builder
- Collect nodes/pods/events/logs; truncate and summarize for context window.
- Add retrieval of relevant snippets (grep logs).

## Phase 5 — Multi-step healing loop
- Plan -> act -> verify -> retry/backoff -> rollback on failure.
- Persist trajectory (messages, tool calls, results) for analysis.

## Phase 6 — Evaluation harness
- Automated cycles: inject -> agent -> verify SLO (pods Ready, endpoints).
- Metrics: success rate, time-to-heal, token cost, unsafe actions.

## Phase 7 — Hardening
- Config, logging, tests, docs (DESIGN.md), CI smoke tests.

## Open questions
- Which faults first? (start with pod-level)
- How much autonomy vs approval?
- Context size limits and summarization strategy.
- How to evaluate "correct fix" objectively.

## Immediate next steps
1. Write feedback.md from verbal feedbacks.
2. Draft DESIGN.md for Phase 1.
3. Implement LLM client abstraction + one tool.

---

# Deep plan (multi-pass)

## A. What we are actually building
A reproducible benchmark + agent that:
1. Breaks a disposable etcd-backed cluster in deterministic ways.
2. Lets a model investigate using only read-only primitives.
3. Requires a structured diagnosis before any repair.
4. Gates repairs against per-scenario allow/forbid lists.
5. Verifies recovery objectively.
6. Records a full trace + score.
7. Resumes after crashes (checkpointer).
8. Compares small local model vs hosted baseline.

## B. Where to start (first concrete artifact)
- A 3-node etcd Docker Compose cluster with static peer config + per-node volumes.
- `reset.sh`: down -v && up -d && wait healthy.
- One fault: etcd-member-down (stop one member, data intact).
- One verify: etcd_healthy_3_members.sh (exit 0).
- Manual inject -> verify -> reset cycle proven by hand before any agent.

## C. How to progress (incremental, each step testable alone)
1. Cluster substrate + reset (no agent).
2. Fault scripts + verify scripts (no agent).
3. LLM client abstraction (local + free endpoints) with one trivial tool.
4. Diagnostic tool layer (read-only, truncated) wired to agent runner.
5. Trace recorder + budget enforcer.
6. submit_diagnosis + gated repair surface.
7. Full loop on one scenario.
8. Remaining faults (determinism check each).
9. Checkpointer + wrong-repair trials.
10. Cross-backend sweep + score table.
11. Publish.

## D. Prerequisites first
- Docker available in the VM (check `docker run hello-world`).
- zrok/Kaggle tunnel tested end-to-end (SSH into notebook).
- One hosted backend API key available (Groq/OpenRouter/Cloudflare).
- Local model runnable (Ollama) for quick iteration.

## E. Automation of each step
- Fault injection: bash scripts with args (node, flags).
- Reset: one command, idempotent, no prompts.
- Verify: exit code + timeout.
- Harness loop: Python orchestrates reset -> inject -> agent -> verify -> record.
- Traces: JSONL per run, append-only.
- Score: aggregate JSONL into table (script).
- Checkpointer: SQLite keyed by run_id; resume by loading messages + tool history.

## F. Resume on failure
- Every run has run_id and checkpoint row.
- On crash: harness reloads checkpoint, skips completed tool calls? Simpler: restart agent with saved message history and budget remainder.
- If cluster unknown: reset first, then mark run aborted (no partial credit).
- Write record before verify can hang (avoid losing results).

## G. Safety
- Faults only on disposable cluster.
- Repair gate checks acceptable/forbidden per scenario.
- Destructive repairs require approval token.
- dry_run mode for all repair tools.
- Never expose raw shell to model; named primitives only.
- Output truncation to protect small model context.

## H. Evaluation
- Diagnosis accuracy (label match).
- Repair correctness (acceptable vs forbidden).
- Recovery (verify exit).
- Safety (zero forbidden actions; control scenario no-action rate).
- Efficiency (steps, tokens, wall time).
- Report rates over N runs, not single anecdotes.

## I. Future expansion (after etcd works)
- LB faults (haproxy/nginx/envoy down, failover).
- Node join/token faults.
- Network partitions, resource exhaustion.
- Wrap diagnostic surface as MCP server.
- Use kubemcp as provisioning fallback if needed.

## J. Decision points to lock before coding
1. Cluster substrate: Docker Compose etcd (default) vs Multipass.
2. Transport: direct tool-calling client (default) vs MCP.
3. Score schema: JSONL (default).
4. Backends: Kaggle small model + one hosted baseline.
5. Budget: max_steps=40, max_tokens=60000 for both.
