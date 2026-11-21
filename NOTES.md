# Project notes — k8s fault-healing agent

## Goal
Build an AI agent from scratch that diagnoses and fixes injected faults in a Kubernetes cluster.

## LLM strategy
- Self-host a small LLM locally or on a Kaggle GPU box, tunneled to.
- Also use free LLM endpoints as fallbacks (no paid subscriptions).

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
- Run myzrok.txt in Kaggle notebook cell to install sshd + zrok2 and share SSH (port 22) as private tunnel.
- Get token from notebook output, then locally: `zrok access private <token>` (one terminal).
- Then SSH: `ssh -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -i ~/.ssh/id_rsa -p 9191 root@127.0.0.1`.
- Need consolidate into smooth scripts.
