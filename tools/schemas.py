"""Tool schemas advertised to the LLM.

Node names are discovered at runtime so the harness works on clusters with
arbitrary hostnames.
"""
from __future__ import annotations

from . import cluster_config

ROOT_CAUSE_LABELS = [
    "no_fault",
    "etcd_member_unreachable",
    "etcd_member_data_loss",
    "etcd_quorum_loss",
    "etcd_disk_exhaustion",
    "etcd_config_error",
]


def node_enum() -> list[str]:
    return cluster_config.nodes()


def build_tools() -> list[dict]:
    nodes = node_enum()

    def fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
        return {"type": "function", "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        }}

    def node_prop(default: str | None = None) -> dict:
        prop = {"type": "string", "enum": nodes}
        if default:
            prop["default"] = default
        return prop

    probe = cluster_config.default_node()
    return [
        fn("kubectl_get", "Get Kubernetes resources.", {"resource": {"type": "string"}, "namespace": {"type": "string"}}, ["resource"]),
        fn("kubectl_describe", "Describe a Kubernetes resource.", {"resource": {"type": "string"}, "name": {"type": "string"}, "namespace": {"type": "string"}}, ["resource", "name"]),
        fn("kubectl_logs", "Get pod logs.", {"pod": {"type": "string"}, "container": {"type": "string"}, "tail_lines": {"type": "integer"}, "namespace": {"type": "string"}}, ["pod"]),
        fn("kubectl_events", "Get namespace events.", {"namespace": {"type": "string"}}, []),
        fn("etcdctl_endpoint_health", "Check etcd endpoint health across the cluster.", {}, []),
        fn("etcdctl_endpoint_status", "Get etcd endpoint status from a node.", {"node": node_prop(probe)}, []),
        fn("etcdctl_member_list", "List etcd members from a node.", {"node": node_prop(probe)}, []),
        fn("etcdctl_alarm_list", "List etcd alarms from a node.", {"node": node_prop(probe)}, []),
        fn("journalctl_tail", "Tail a systemd unit journal on a node.", {"node": node_prop(), "unit": {"type": "string"}, "lines": {"type": "integer"}}, ["node", "unit"]),
        fn("crictl_ps", "List containers on a node.", {"node": node_prop()}, ["node"]),
        fn("crictl_logs", "Get container logs on a node.", {"node": node_prop(), "container_id": {"type": "string"}, "tail": {"type": "integer"}}, ["node", "container_id"]),
        fn("systemctl_status", "Get systemd unit status on a node.", {"node": node_prop(), "unit": {"type": "string"}}, ["node", "unit"]),
        fn("df_path", "Check disk usage for a path on a node.", {"node": node_prop(), "path": {"type": "string"}}, ["node", "path"]),
        fn("ls_path", "List a path on a node.", {"node": node_prop(), "path": {"type": "string"}}, ["node", "path"]),
        fn("submit_diagnosis", "Submit one root cause label. Required before repairs; returns an approval token if correct.", {"root_cause": {"type": "string", "enum": ROOT_CAUSE_LABELS}, "affected": {"type": "array", "items": {"type": "string", "enum": nodes}}}, ["root_cause"]),
        fn("restart_member", "Restart an etcd member on a node. Requires approval_token from submit_diagnosis.", {"node": node_prop(), "approval_token": {"type": "string"}}, ["node", "approval_token"]),
        fn("member_remove_readd", "Remove and re-add an etcd member on a node. Requires approval_token.", {"node": node_prop(), "approval_token": {"type": "string"}}, ["node", "approval_token"]),
        fn("kube_etcd_switch_rebuild", "Rebuild an etcd member via kube-etcd-switch. Requires approval_token.", {"node": node_prop(), "approval_token": {"type": "string"}}, ["node", "approval_token"]),
        fn("snapshot_restore_full", "Restore etcd from full snapshot. Requires approval_token.", {"approval_token": {"type": "string"}}, ["approval_token"]),
        fn("reset_cluster", "Reset the whole cluster. Requires approval_token.", {"approval_token": {"type": "string"}}, ["approval_token"]),
    ]


REPAIR_TOOLS = {"restart_member", "member_remove_readd", "kube_etcd_switch_rebuild", "snapshot_restore_full", "reset_cluster"}
DIAGNOSIS_TOOL = "submit_diagnosis"
