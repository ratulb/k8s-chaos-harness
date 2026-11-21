from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

RECOMMENDED_MEMBERS = 3
ORDINAL_ROLES = (
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
    "eighth", "ninth", "tenth", "eleventh", "twelfth", "thirteenth",
    "fourteenth", "fifteenth", "sixteenth", "seventeenth", "eighteenth",
    "nineteenth", "twentieth",
)
_FROZEN_TOPOLOGY: dict[str, Any] | None = None


def ordinal_name(index: int) -> str:
    if 1 <= index <= len(ORDINAL_ROLES):
        return ORDINAL_ROLES[index - 1]
    return f"node_{index}"


def ordinal_index(value: str) -> int | None:
    text = str(value).strip().lower()
    if text.isdigit():
        return int(text) if int(text) >= 1 else None
    if text in ORDINAL_ROLES:
        return ORDINAL_ROLES.index(text) + 1
    return None


def quorum_for(member_count: int) -> int:
    return member_count // 2 + 1


def quorum_loss_target_count(member_count: int) -> int:
    return member_count - member_count // 2


def _sha256(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in result:
            result.append(text)
    return result


def _run_json(cmd: list[str]) -> Any | None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _setup_conf_hosts() -> dict[str, list[str]]:
    install_dir = Path(os.environ.get("K8S_EASY_INSTALL", Path.home() / "k8s-easy-install"))
    try:
        lines = (install_dir / "setup.conf").read_text(encoding="utf-8").splitlines()
    except Exception:
        return {}
    result: dict[str, list[str]] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        try:
            result[key.strip()] = shlex.split(value, comments=True)
        except ValueError:
            result[key.strip()] = value.split()
    return result


def setup_conf_summary() -> dict[str, Any]:
    conf = _setup_conf_hosts()
    install_dir = Path(os.environ.get("K8S_EASY_INSTALL", Path.home() / "k8s-easy-install"))
    path = install_dir / "setup.conf"
    normalized = {
        "masters": sorted(conf.get("masters") or []),
        "workers": sorted(conf.get("workers") or []),
        "loadbalancer": sorted(conf.get("loadbalancer") or []),
    }
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode("utf-8")).hexdigest()
    return {
        "path": str(path),
        "exists": path.exists(),
        "masters": list(conf.get("masters") or []),
        "workers": list(conf.get("workers") or []),
        "loadbalancer": list(conf.get("loadbalancer") or []),
        "hash": digest,
    }


def _cache_map() -> dict[str, str]:
    path = Path.home() / ".cache" / "k8s-chaos-harness" / "node_ips.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def _env_ip_map() -> dict[str, str]:
    result: dict[str, str] = {}
    for part in os.environ.get("K8S_NODE_IPS", "").split(","):
        if "=" in part:
            name, ip = part.split("=", 1)
            if name.strip() and ip.strip():
                result[name.strip()] = ip.strip()
    return result


def _node_details_from_k8s(items: list[dict]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for item in items:
        metadata = item.get("metadata", {}) or {}
        name = str(metadata.get("name", "")).strip()
        if not name:
            continue
        labels = metadata.get("labels", {}) or {}
        annotations = metadata.get("annotations", {}) or {}
        addresses = (item.get("status", {}) or {}).get("addresses", []) or []
        ip = next(
            (str(address.get("address", "")) for address in addresses
             if str(address.get("type", "")) == "InternalIP" and address.get("address")),
            "",
        )
        if not ip:
            ip = str(annotations.get("kubernetes.io/hostname", ""))
        ready = any(
            str(condition.get("type", "")) == "Ready"
            and str(condition.get("status", "")) == "True"
            for condition in ((item.get("status", {}) or {}).get("conditions", []) or [])
        )
        control_plane = (
            "node-role.kubernetes.io/control-plane" in labels
            or "node-role.kubernetes.io/master" in labels
        )
        details.append({
            "name": name,
            "ip": ip,
            "ready": ready,
            "control_plane": control_plane,
            "confidence": "confirmed",
        })
    return sorted(details, key=lambda detail: detail["name"])


def _pod_details(items: list[dict], node_names: list[str]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for item in items:
        metadata = item.get("metadata", {}) or {}
        name = str(metadata.get("name", "")).strip()
        if not name:
            continue
        node = str((item.get("spec", {}) or {}).get("nodeName", "")).strip()
        if not name.startswith("etcd-") and node not in node_names:
            continue
        ready = str((item.get("status", {}) or {}).get("phase", "")) == "Running"
        statuses = (item.get("status", {}) or {}).get("containerStatuses", []) or []
        if statuses:
            ready = ready and all(str(status.get("ready", False)).lower() == "true" for status in statuses)
        details.append({"name": name, "node": node, "ready": ready})
    return sorted(details, key=lambda detail: detail["name"])


def _parse_etcd_members(text: str) -> list[dict[str, Any]]:
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = []
        raw = payload.get("members", []) if isinstance(payload, dict) else payload
        result: list[dict[str, Any]] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            peers = item.get("peerURLs") or item.get("peerUrls") or item.get("peer_url") or []
            if isinstance(peers, str):
                peers = [peers]
            member_id = str(item.get("ID") or item.get("id") or item.get("member") or "").strip()
            result.append({
                "id": member_id or name,
                "name": name,
                "peer_url": str(peers[0]).strip() if peers else "",
            })
        return result


    result = []
    for line in stripped.splitlines():
        match = re.search(r"\b([0-9a-fA-F]{8,})[,|\s]+name=([^,|\s]+)", line)
        if not match:
            match = re.search(r"\b([0-9a-fA-F]{8,})\s*\|\s*([^|,\s]+)", line)
        if not match:
            continue
        peer = re.search(r"peerURLs=([^,|\s]+)", line)
        result.append({
            "id": match.group(1),
            "name": match.group(2).strip("= "),
            "peer_url": peer.group(1) if peer else "",
        })
    return result


def _etcdctl_member_list(pod: str) -> list[dict[str, Any]]:
    base = [
        "kubectl", "--request-timeout=10s", "exec", "-n", "kube-system", pod,
        "--", "etcdctl",
        "--endpoints=https://127.0.0.1:2379",
        "--cacert=/etc/kubernetes/pki/etcd/ca.crt",
        "--cert=/etc/kubernetes/pki/etcd/server.crt",
        "--key=/etc/kubernetes/pki/etcd/server.key",
    ]
    try:
        proc = subprocess.run([*base, "--write-out=json", "member", "list"], capture_output=True, text=True, check=False, timeout=30)
    except Exception:
        return []
    if proc.returncode != 0:
        try:
            proc = subprocess.run([*base, "member", "list"], capture_output=True, text=True, check=False, timeout=30)
        except Exception:
            return []
    return _parse_etcd_members(proc.stdout or "")


def _normalize_members(raw: list[Any], control_planes: list[str], ips: dict[str, str]) -> list[dict[str, Any]]:
    members: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if isinstance(item, str):
            name = item
            member_id = ""
            peer_url = ""
            node = item
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("node") or "").strip()
            member_id = str(item.get("id") or "").strip()
            peer_url = str(item.get("peer_url") or item.get("peerURL") or "").strip()
            node = str(item.get("node") or name).strip()
        else:
            continue
        if not name and index < len(control_planes):
            name = control_planes[index]
        if not name:
            continue
        node = node or name
        members.append({
            "id": member_id or f"inferred-{name}",
            "name": name,
            "node": node if node in control_planes else (node or name),
            "peer_url": peer_url or (f"https://{ips[node]}:2380" if ips.get(node) else ""),
            "confidence": "confirmed" if member_id else "inferred",
        })
    return members


def _assemble(
    *,
    nodes: list[str],
    control_planes: list[str],
    node_details: list[dict[str, Any]],
    members: list[dict[str, Any]],
    source: str,
    confidence: str,
    warnings: list[str],
    setup_hosts: list[str],
) -> dict[str, Any]:
    all_nodes = _dedupe(nodes)
    if not all_nodes:
        raise RuntimeError("cannot discover any cluster nodes")
    control_planes = _dedupe(control_planes) or all_nodes
    detail_by_name = {str(detail.get("name", "")): detail for detail in node_details}
    for name in all_nodes:
        detail_by_name.setdefault(name, {"name": name, "ip": "", "ready": True, "control_plane": name in control_planes, "confidence": "inferred"})
    details = [detail_by_name[name] for name in all_nodes if name in detail_by_name]
    ips = {str(detail.get("name")): str(detail.get("ip") or "") for detail in details}
    if not members:
        members = _normalize_members(control_planes, control_planes, ips)
    probe_override = os.environ.get("PROBE_NODE", "").strip()
    probe_node = probe_override if probe_override in all_nodes else control_planes[0]
    member_count = len(members)
    quorum = quorum_for(member_count)
    loss_targets = quorum_loss_target_count(member_count)
    ordered_targets = [name for name in control_planes if name in {member["node"] for member in members}]
    if len(ordered_targets) < loss_targets:
        ordered_targets = list(control_planes)
    ordered_targets = ordered_targets[-loss_targets:] if loss_targets else []
    identity = {
        "control_planes": sorted(
            ({"name": name, "ip": ips.get(name, "")} for name in control_planes),
            key=lambda item: (item["name"], item["ip"]),
        ),
        "etcd_members": sorted(
            (
                {"id": member["id"], "name": member["name"], "node": member["node"], "peer_url": member["peer_url"]}
                for member in members
            ),
            key=lambda item: (item["name"], item["id"], item["node"], item["peer_url"]),
        ),
        "member_count": member_count,
        "probe_node": probe_node,
    }
    return {
        "source": source,
        "confidence": confidence,
        "warnings": _dedupe(warnings),
        "all_nodes": all_nodes,
        "control_planes": control_planes,
        "node_details": details,
        "etcd_members": members,
        "etcd_member_count": member_count,
        "quorum": quorum,
        "quorum_loss_targets": loss_targets,
        "quorum_loss_nodes": ordered_targets[:loss_targets],
        "probe_node": probe_node,
        "setup_conf_hash": _sha256(sorted(set(setup_hosts))),
        "topology_hash": _sha256(identity),
    }


def _from_fixture(payload: dict[str, Any]) -> dict[str, Any]:
    raw_details = payload.get("node_details") or []
    details: list[dict[str, Any]] = []
    for item in raw_details:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        details.append({
            "name": name,
            "ip": str(item.get("ip") or "").strip(),
            "ready": bool(item.get("ready", True)),
            "control_plane": bool(item.get("control_plane", True)),
            "confidence": str(item.get("confidence") or "confirmed"),
        })
    ip_map = {str(key): str(value) for key, value in (payload.get("node_ips") or {}).items()}
    names = _dedupe([str(item) for item in (payload.get("nodes") or payload.get("all_nodes") or [])])
    if not names and details:
        names = [str(detail["name"]) for detail in details]
    if not details:
        for name in names:
            details.append({
                "name": name,
                "ip": ip_map.get(name, ""),
                "ready": True,
                "control_plane": True,
                "confidence": "confirmed",
            })
    control_planes = _dedupe([
        str(item.get("name") if isinstance(item, dict) else item)
        for item in (payload.get("control_planes") or names)
    ])
    ips = {str(detail.get("name")): str(detail.get("ip") or "") for detail in details}
    members = _normalize_members(payload.get("etcd_members") or [], control_planes, ips)
    return _assemble(
        nodes=names,
        control_planes=control_planes,
        node_details=details,
        members=members,
        source=str(payload.get("source") or "fixture"),
        confidence=str(payload.get("confidence") or "confirmed"),
        warnings=[str(item) for item in (payload.get("warnings") or [])],
        setup_hosts=[str(item) for item in (payload.get("setup_hosts") or [])],
    )


def _from_live() -> dict[str, Any] | None:
    node_payload = _run_json(["kubectl", "--request-timeout=10s", "get", "nodes", "-o", "json"])
    if not isinstance(node_payload, dict) or not node_payload.get("items"):
        return None
    details = _node_details_from_k8s(node_payload["items"])
    if not details:
        return None
    pod_items: list[dict] = []
    pod_payload = _run_json([
        "kubectl", "--request-timeout=10s", "get", "pods", "-n", "kube-system",
        "-l", "component=etcd", "-o", "json",
    ])
    if isinstance(pod_payload, dict):
        pod_items = pod_payload.get("items", [])
    if not pod_items:
        fallback = _run_json([
            "kubectl", "--request-timeout=10s", "get", "pods", "-n", "kube-system", "-o", "json",
        ])
        if isinstance(fallback, dict):
            pod_items = [item for item in fallback.get("items", []) if str((item.get("metadata") or {}).get("name", "")).startswith("etcd-")]
    all_nodes = [detail["name"] for detail in details]
    pods = _pod_details(pod_items, all_nodes)
    labelled = [detail["name"] for detail in details if detail["control_plane"]]
    pod_nodes = _dedupe([pod["node"] for pod in pods if pod["node"]])
    control_planes = _dedupe(labelled or pod_nodes or all_nodes)
    for detail in details:
        detail["control_plane"] = detail["name"] in control_planes
    ips = {detail["name"]: detail["ip"] for detail in details}
    members = _normalize_members(pods, control_planes, ips)
    live_members: list[dict[str, Any]] = []
    for pod in sorted(pods, key=lambda item: (not item["ready"], item["name"])):
        live_members = _etcdctl_member_list(pod["name"])
        if live_members:
            break
    if live_members:
        by_key: dict[str, dict[str, Any]] = {}
        for member in members:
            for key in (member.get("name"), member.get("node")):
                if key:
                    by_key[str(key)] = member
        for member in live_members:
            existing = by_key.get(str(member["name"])) or by_key.get(str(member.get("node", "")))
            if existing:
                existing.update(member)
                existing["confidence"] = "confirmed"
            else:
                members.append({
                    "id": member["id"],
                    "name": member["name"],
                    "node": member["name"] if member["name"] in control_planes else control_planes[0],
                    "peer_url": member["peer_url"],
                    "confidence": "confirmed",
                })
    warnings: list[str] = []
    confidence = "confirmed" if live_members else "inferred"
    if not live_members:
        warnings.append("etcd member identities could not be confirmed; using pod-derived inference")
    return _assemble(
        nodes=all_nodes,
        control_planes=control_planes,
        node_details=details,
        members=members,
        source="live",
        confidence=confidence,
        warnings=warnings,
        setup_hosts=_setup_conf_hosts().get("masters", []),
    )


def _from_fallback() -> dict[str, Any]:
    configured = _dedupe(os.environ.get("K8S_NODES", "").replace(",", " ").split())
    cache = _cache_map()
    setup_hosts = [str(item) for item in _setup_conf_hosts().get("masters", [])]
    if configured:
        names = configured
        source = "env"
        confidence = "inferred"
    elif cache:
        names = _dedupe(sorted(cache))
        source = "cache"
        confidence = "stale"
    elif setup_hosts:
        names = _dedupe(setup_hosts)
        source = "setup_conf"
        confidence = "inferred"
    else:
        raise RuntimeError("cannot discover topology: kubectl unavailable and no K8S_NODES/cache/setup.conf hosts")
    overrides = {**cache, **_env_ip_map()}
    details = [{
        "name": name,
        "ip": overrides.get(name, ""),
        "ready": True,
        "control_plane": True,
        "confidence": confidence,
    } for name in names]
    raw_members: list[Any] = []
    configured_members = os.environ.get("K8S_ETCD_MEMBERS", "").strip()
    if configured_members:
        for part in configured_members.replace(",", " ").split():
            pieces = part.split("=")
            raw_members.append({
                "name": pieces[0],
                "id": pieces[1] if len(pieces) > 1 else "",
                "peer_url": pieces[2] if len(pieces) > 2 else "",
                "node": pieces[0],
            })
    warnings = [f"topology is {confidence}; live Kubernetes discovery was unavailable"]
    return _assemble(
        nodes=names,
        control_planes=names,
        node_details=details,
        members=_normalize_members(raw_members, names, overrides),
        source=source,
        confidence=confidence,
        warnings=warnings,
        setup_hosts=setup_hosts,
    )


def discover_topology() -> dict[str, Any]:
    fixture = os.environ.get("CHAOS_TOPOLOGY_JSON", "").strip()
    if fixture:
        if fixture.lstrip().startswith("{"):
            payload = json.loads(fixture)
        else:
            payload = json.loads(Path(fixture).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("CHAOS_TOPOLOGY_JSON must contain an object")
        return _from_fixture(payload)
    return _from_live() or _from_fallback()


def freeze_topology(topology: dict[str, Any]) -> dict[str, Any]:
    global _FROZEN_TOPOLOGY
    _FROZEN_TOPOLOGY = copy.deepcopy(topology)
    try:
        from tools import cluster_config
        cluster_config.nodes.cache_clear()
    except Exception:
        pass
    return copy.deepcopy(_FROZEN_TOPOLOGY)


def frozen_topology() -> dict[str, Any] | None:
    return copy.deepcopy(_FROZEN_TOPOLOGY)


def clear_frozen_topology() -> None:
    global _FROZEN_TOPOLOGY
    _FROZEN_TOPOLOGY = None
    try:
        from tools import cluster_config
        cluster_config.nodes.cache_clear()
    except Exception:
        pass


def local_node_names(topology: dict[str, Any], local_node: str | None = None) -> set[str]:
    identifiers: set[str] = set()
    if local_node:
        identifiers.add(str(local_node).strip())
    else:
        override = os.environ.get("CHAOS_LOCAL_NODE", "").strip()
        if override:
            identifiers.add(override)
        try:
            hostname = socket.gethostname()
            identifiers.update({hostname, hostname.split(".")[0]})
        except OSError:
            pass
        try:
            identifiers.update(item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None))
        except OSError:
            pass
        try:
            output = subprocess.check_output(["hostname", "-I"], text=True, timeout=5)
            identifiers.update(output.split())
        except Exception:
            pass
    identifiers.discard("")
    names: set[str] = set()
    control_planes = list(topology.get("control_planes") or [])
    all_nodes = list(topology.get("all_nodes") or control_planes)
    for detail in topology.get("node_details") or []:
        name = str(detail.get("name") or "").strip()
        ip = str(detail.get("ip") or "").strip()
        if (name and name in identifiers) or (ip and ip in identifiers):
            if name:
                names.add(name)
    for identifier in identifiers:
        if identifier in all_nodes:
            names.add(identifier)
    return names


def resolve_targets(
    topology: dict[str, Any],
    selectors: Any,
    *,
    local_node: str | None = None,
    avoid_local: bool = False,
) -> list[str]:
    if selectors is None:
        selectors = []
    if isinstance(selectors, str):
        selectors = [selectors]
    control_planes = list(topology.get("control_planes") or [])
    all_nodes = list(topology.get("all_nodes") or control_planes)
    loss_nodes = list(topology.get("quorum_loss_nodes") or [])
    local_names = local_node_names(topology, local_node) if avoid_local else set()
    if avoid_local:
        candidate_planes = [name for name in control_planes if name not in local_names]
        loss_nodes = [name for name in loss_nodes if name not in local_names]
        if not candidate_planes and any(ordinal_index(item) is not None for item in selectors):
            raise RuntimeError("no non-local control-plane target; refusing to target local harness host")
    else:
        candidate_planes = control_planes
    targets: list[str] = []
    for selector in selectors:
        text = str(selector).strip()
        if text == "quorum_losing_set":
            if not loss_nodes:
                raise RuntimeError("quorum_losing_set has no non-local target node")
            targets.extend(loss_nodes)
            continue
        index = ordinal_index(text)
        if index is not None:
            if index > len(candidate_planes):
                raise RuntimeError(f"selector {text!r} exceeds {len(candidate_planes)} control-plane node(s)")
            targets.append(candidate_planes[index - 1])
            continue
        if text in all_nodes:
            if avoid_local and text in local_names:
                raise RuntimeError(f"target {text!r} is the local harness host")
            targets.append(text)
            continue
        raise RuntimeError(f"unknown target selector {text!r}; expected a node name, role, index, or quorum_losing_set")
    return _dedupe(targets)


def capability_check(
    scenario: dict[str, Any],
    topology: dict[str, Any],
    mode: str = "benchmark",
    local_node: str | None = None,
) -> dict[str, Any]:
    if mode not in {"benchmark", "repair"}:
        raise RuntimeError(f"unknown mode {mode!r}")
    warnings = [str(item) for item in topology.get("warnings") or []]
    member_count = int(topology.get("etcd_member_count") or len(topology.get("etcd_members") or []))
    minimum = int(scenario.get("min_etcd_members") or 1)
    impact_class = str(scenario.get("impact_class") or "control")
    selectors = scenario.get("target_nodes") or []
    if isinstance(selectors, str):
        selectors = [selectors]
    dynamic_quorum = any(str(item) == "quorum_losing_set" for item in selectors)
    avoid_local = bool(scenario.get("avoid_local_target"))

    if member_count != RECOMMENDED_MEMBERS:
        warnings.append(
            f"etcd topology has {member_count} member(s); {RECOMMENDED_MEMBERS} is the loud recommendation"
        )
    if member_count < RECOMMENDED_MEMBERS:
        warnings.append("best-effort mode: malformed and small-etcd failure modes may differ from a 3-member cluster")
    if member_count > 0 and member_count % 2 == 0:
        warnings.append("even etcd member count has no tie-break advantage and may lose quorum more easily")

    eligible = True
    reason: str | None = None
    quorum_related = impact_class == "quorum_loss" or dynamic_quorum
    if quorum_related and member_count == 1:
        impact_class = "total_outage"
        if mode == "benchmark":
            eligible = False
            reason = "single-member quorum loss is total outage; not scored in benchmark mode"
        else:
            warnings.append("quorum loss on one member is total outage; repair is explicit best-effort only")
    elif member_count == 1 and impact_class == "single_member_fault":
        impact_class = "total_outage"
        warnings.append("single-member fault is total outage on this topology")

    if member_count < minimum:
        if mode == "benchmark" and eligible:
            eligible = False
            reason = f"scenario requires at least {minimum} etcd member(s); topology has {member_count}"
        else:
            warnings.append(f"topology is below scenario minimum of {minimum} etcd member(s); repair is best-effort")

    if member_count < 1:
        eligible = False
        reason = reason or "topology has no etcd members"

    if avoid_local:
        local_names = local_node_names(topology, local_node)
        try:
            resolved = resolve_targets(
                topology,
                selectors,
                local_node=local_node,
                avoid_local=True,
            )
        except RuntimeError as exc:
            if eligible and mode == "benchmark":
                eligible = False
                reason = reason or str(exc)
            else:
                warnings.append(str(exc))
        else:
            joined = ", ".join(sorted(local_names)) or "detected host"
            warnings.append(f"target selection avoids local harness host: {joined}")
            warnings.append(f"resolved target(s): {', '.join(resolved)}")

    return {
        "eligible": eligible,
        "warning": _dedupe(warnings),
        "unsupported_reason": reason,
        "impact_class": impact_class,
        "mode": mode,
        "member_count": member_count,
        "quorum": quorum_for(member_count) if member_count else 0,
        "quorum_loss_targets": quorum_loss_target_count(member_count),
        "topology_hash": str(topology.get("topology_hash") or ""),
    }


def summary(topology: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": topology.get("source"),
        "confidence": topology.get("confidence"),
        "warnings": topology.get("warnings", []),
        "all_nodes": topology.get("all_nodes", []),
        "control_planes": topology.get("control_planes", []),
        "etcd_members": topology.get("etcd_members", []),
        "etcd_member_count": topology.get("etcd_member_count"),
        "quorum": topology.get("quorum"),
        "quorum_loss_targets": topology.get("quorum_loss_targets"),
        "quorum_loss_nodes": topology.get("quorum_loss_nodes", []),
        "probe_node": topology.get("probe_node"),
        "topology_hash": topology.get("topology_hash"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show")
    show.add_argument("--json", action="store_true")
    sub.add_parser("quorum")
    sub.add_parser("quorum-set")
    sub.add_parser("hash")
    capability = sub.add_parser("capability")
    capability.add_argument("scenario_id")
    capability.add_argument("--mode", choices=["benchmark", "repair"], default="benchmark")
    args = parser.parse_args()

    topology = discover_topology()
    if args.command == "show":
        if args.json:
            print(json.dumps(summary(topology), indent=2, sort_keys=True))
        else:
            print(f"source: {topology['source']} ({topology['confidence']})")
            print(f"hash: {topology['topology_hash']}")
            print(f"control planes: {', '.join(topology['control_planes'])}")
            print(f"etcd members: {topology['etcd_member_count']}")
            print(f"quorum: {topology['quorum']} of {topology['etcd_member_count']}")
            print(f"quorum-loss targets: {', '.join(topology['quorum_loss_nodes'])}")
            for warning in topology["warnings"]:
                print(f"warning: {warning}")
    elif args.command == "quorum":
        print(topology["quorum"])
    elif args.command == "quorum-set":
        print("\n".join(topology["quorum_loss_nodes"]))
    elif args.command == "hash":
        print(topology["topology_hash"])
    elif args.command == "capability":
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root))
        from tools.scenario_schema import load_scenario
        scenario = load_scenario(root, args.scenario_id)
        print(json.dumps(capability_check(scenario, topology, args.mode), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
