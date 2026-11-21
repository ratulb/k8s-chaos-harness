from __future__ import annotations

import os
import subprocess
from typing import Any


def _values(name: str) -> set[str]:
    return {item.strip() for item in os.environ.get(name, "").split(",") if item.strip()}


def current_context() -> str:
    override = os.environ.get("CHAOS_CLUSTER_ID", "").strip()
    if override:
        return override
    try:
        proc = subprocess.run(
            ["kubectl", "config", "current-context"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except Exception:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def require_destructive_guard(topology: dict[str, Any] | None = None) -> dict[str, Any]:
    topology = topology or {}
    context = current_context()
    cluster_id = os.environ.get("CHAOS_CLUSTER_ID", "").strip() or context
    topology_hash = str(topology.get("topology_hash") or os.environ.get("CHAOS_TOPOLOGY_HASH", ""))
    allowed_contexts = sorted(_values("CHAOS_ALLOWED_CLUSTER_CONTEXTS"))
    allowed_ids = sorted(_values("CHAOS_ALLOWED_CLUSTER_IDS"))
    allowed_hashes = sorted(_values("CHAOS_ALLOWED_TOPOLOGY_HASHES"))

    identity_ok = bool(context and context in allowed_contexts) or bool(
        cluster_id and cluster_id in allowed_ids
    )
    topology_ok = not allowed_hashes or (bool(topology_hash) and topology_hash in allowed_hashes)
    payload = {
        "enforced": True,
        "context": context,
        "cluster_id": cluster_id,
        "topology_hash": topology_hash,
        "allowed_contexts": allowed_contexts,
        "allowed_cluster_ids": allowed_ids,
        "allowed_topology_hashes": allowed_hashes,
    }
    if not identity_ok:
        raise RuntimeError(
            "destructive benchmark refused: current kubectl context "
            f"{context or '<unknown>'!r} and cluster id {cluster_id or '<unknown>'!r} "
            "are not allow-listed; set CHAOS_ALLOWED_CLUSTER_CONTEXTS or "
            "CHAOS_ALLOWED_CLUSTER_IDS"
        )
    if not topology_ok:
        raise RuntimeError(
            "destructive benchmark refused: topology hash "
            f"{topology_hash or '<unknown>'!r} is not allow-listed; set "
            "CHAOS_ALLOWED_TOPOLOGY_HASHES"
        )
    return payload
