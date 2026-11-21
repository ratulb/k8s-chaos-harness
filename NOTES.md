# Project notes — k8s fault-healing agent

`DESIGN.md` is the canonical, reconciled design. `PHASES.md` is the current
status and action plan. This file keeps historical context and quick reminders.

## Goal
Build an AI agent from scratch that diagnoses and fixes injected faults in a Kubernetes cluster.

## LLM strategy
- Use an Ollama-compatible endpoint as the self-hosted backend, local or tunneled.
- A Kaggle GPU notebook is optional infrastructure for that endpoint.
- Groq, OpenRouter, and Cloudflare are hosted baselines, not automatic fallbacks.
- One backend is selected per run.

## Cluster
- This repo spins up multi-node K8s clusters (kubeadm + Calico VXLAN).
- Faults will be injected; agent consults LLM for fix ideas.

## Process
1. Dump verbal feedbacks to feedback.md (may include stale items; ignore invalid).
2. Start small, step by step.
3. First deliverable: DESIGN.md.

## Current cluster status
- 3 control-plane nodes with stacked etcd, all Ready.
- Node names and addresses are discovered at runtime from Kubernetes; no fixed hostnames or IPs are baked into the harness.
- Calico 1/1, cross-namespace pod connectivity verified.

## Reference: tool_call_loop (cloned at /home/tool_call_loop)
- Minimal ~100-line Python tool-calling loop on local Ollama (refinedneuro/refinedtoolcallv5-3b, 3B, CPU).
- Demonstrates model emits tool request; code executes; result returned; model answers.
- Free endpoint variants: Cloudflare Workers AI, Groq, OpenRouter (many :free models).
- Key lesson: working tool call != trustworthy answer; validate outputs.

## Reference: kube-etcd-switch (cloned at /home/kube-etcd-switch)
- Bash tooling for zero-downtime migration between embedded (static-pod) and external (systemd) etcd in kubeadm clusters.
- Snapshot-driven (etcdutl snapshot save/restore), reversible, agentless via SSH.
- Can nuke/restore etcd ("brain of k8s") for fault-injection/disaster-recovery experiments.
- Blog: https://ratulb.github.io/techcottage/2021/07/migrate-kubernetes-embedded-etcd-to-external-etcd-easy-back-and-forth-switch/

## LLM endpoint abstraction
- Wrap Groq, OpenRouter, Cloudflare Workers AI (see *_tool_call.py), local Ollama, and Kaggle-tunneled small LLM behind one client interface.
- All expose OpenAI-compatible chat completions; differences: base_url, api_key, model name, tool support.

## Kaggle GPU tunneling (zrok)
- Run `kaggle/bootstrap.sh` in a Kaggle notebook cell to install sshd + Ollama and share SSH as a private zrok tunnel.
- The Kaggle `authorized_keys` must contain the public key matching the local private key; otherwise SSH asks for a password.
- Locally: `zrok2 access private <token>` (defaults to `127.0.0.1:9191`).
- Then: `ssh -N -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -i ~/.ssh/id_rsa -p 9191 root@127.0.0.1 -L 11435:127.0.0.1:11434`.
- Set `OLLAMA_BASE_URL=http://127.0.0.1:11435`; full steps are in `kaggle/README.md`.

## SQLite checkpoint store
- `SQLite.md` holds historical reviewer input; `DESIGN.md` is the reconciled source of truth.
- SQLite is authoritative for the active epoch; `runs.jsonl` and `traces/*.jsonl` are regenerated exports.
- Event log is append-only: messages, tool calls, LLM requests, trace events, artifacts, state snapshots, UI stream.
- `runs.state_json` is a cache rebuilt from the log when its seq marker is stale.
- Unknown mutating tool calls abort the original run and create a fresh retry linked by `resume_of`.
- Resumed, interactive, pre-gate, and unsupported runs are excluded from headline rates by default.
- There is no archive. `chaos state discard` removes stale operational state.
- A normal new run auto-discards when live `topology_hash` differs from active SQLite.
- Explicit resume refuses on topology mismatch and never discards.
- A self-hosted endpoint is not required for SQLite work; live endpoints are needed only for model sweeps.

## Latest validation
- Historical: 13/13 unit tests passed before SQLite integration.
- Historical: self-hosted endpoint and hosted backends passed health/readiness at the time.
- Historical: six-scenario sweeps completed over seeds 401-403; infrastructure was stable, but diagnosis accuracy was model-dependent.
- Machines are currently down for cost, so current live validation is incomplete.
