# k8s-chaos-harness

Reproducible etcd fault diagnosis and repair benchmark on a real 3-control-plane kubeadm cluster.

See `DESIGN.md` for the full plan and `PHASES.md` for current status.

## Cluster

The cluster is built from `/home/azureuser/k8s-easy-install`:

- `vm1` / `10.1.0.4`, `vm2` / `10.2.0.4`, `vm3` / `10.3.0.4`
- HAProxy load balancer at `10.3.0.4:8080`
- Kubernetes v1.36.6, stacked etcd, Calico CNI

Useful commands:

```bash
bash cluster/reset_cluster.sh            # full nuke/rebuild + verify + snapshot
bash cluster/snapshot_clean_state.sh     # save clean etcd static-pod manifests
bash cluster/restore_clean_state.sh      # fast restore after a fault
bash cluster/verify_etcd_healthy.sh      # baseline / final verify
```

## Configuration

Copy `.env.example` to `.env` and configure at least one backend. The `kaggle` backend in
`run_benchmark.py` is an Ollama-compatible endpoint, so it can point at a local Ollama,
a Kaggle tunnel, or another compatible server.

## Running

```bash
python3 run_benchmark.py --scenario etcd-member-down --backend groq --runs 1
python3 score.py --format md
```

Scenarios:

- `control-healthy-noisy`
- `etcd-disk-full`
- `etcd-member-down`
- `etcd-member-wiped`
- `etcd-quorum-loss`
- `etcd-wrong-peer-url`

Each run records a JSON line in `runs.jsonl` and a tool-by-tool trace in `traces/`.
Repairs are gated: the agent must submit an exact diagnosis label before any repair tool
is authorized, and forbidden repairs are blocked and logged.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

If the configured `OLLAMA_MODEL` is not present locally, override it for the smoke test:

```bash
OLLAMA_MODEL=qwen3:8b python3 -m unittest discover -s tests -v
```
