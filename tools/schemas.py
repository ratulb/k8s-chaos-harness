from __future__ import annotations

ROOT_CAUSE_LABELS = [
    "no_fault",
    "etcd_member_unreachable",
    "etcd_member_data_loss",
    "etcd_quorum_loss",
    "etcd_disk_exhaustion",
    "etcd_config_error",
]

NODES = ["vm1", "vm2", "vm3"]

TOOLS = [
    {"type": "function", "function": {"name": "kubectl_get", "description": "Get Kubernetes resources.", "parameters": {"type": "object", "properties": {"resource": {"type": "string"}, "namespace": {"type": "string"}}, "required": ["resource"]}}},
    {"type": "function", "function": {"name": "kubectl_describe", "description": "Describe a Kubernetes resource.", "parameters": {"type": "object", "properties": {"resource": {"type": "string"}, "name": {"type": "string"}, "namespace": {"type": "string"}}, "required": ["resource", "name"]}}},
    {"type": "function", "function": {"name": "kubectl_logs", "description": "Get pod logs.", "parameters": {"type": "object", "properties": {"pod": {"type": "string"}, "container": {"type": "string"}, "tail_lines": {"type": "integer"}, "namespace": {"type": "string"}}, "required": ["pod"]}}},
    {"type": "function", "function": {"name": "kubectl_events", "description": "Get namespace events.", "parameters": {"type": "object", "properties": {"namespace": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {"name": "etcdctl_endpoint_health", "description": "Check etcd endpoint health across the cluster.", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "etcdctl_endpoint_status", "description": "Get etcd endpoint status from a node.", "parameters": {"type": "object", "properties": {"node": {"type": "string", "enum": NODES, "default": "vm1"}}, "required": []}}},
    {"type": "function", "function": {"name": "etcdctl_member_list", "description": "List etcd members from a node.", "parameters": {"type": "object", "properties": {"node": {"type": "string", "enum": NODES, "default": "vm1"}}, "required": []}}},
    {"type": "function", "function": {"name": "etcdctl_alarm_list", "description": "List etcd alarms from a node.", "parameters": {"type": "object", "properties": {"node": {"type": "string", "enum": NODES, "default": "vm1"}}, "required": []}}},
    {"type": "function", "function": {"name": "journalctl_tail", "description": "Tail a systemd unit journal on a node.", "parameters": {"type": "object", "properties": {"node": {"type": "string", "enum": NODES}, "unit": {"type": "string"}, "lines": {"type": "integer"}}, "required": ["node", "unit"]}}},
    {"type": "function", "function": {"name": "crictl_ps", "description": "List containers on a node.", "parameters": {"type": "object", "properties": {"node": {"type": "string", "enum": NODES}}, "required": ["node"]}}},
    {"type": "function", "function": {"name": "crictl_logs", "description": "Get container logs on a node.", "parameters": {"type": "object", "properties": {"node": {"type": "string", "enum": NODES}, "container_id": {"type": "string"}, "tail": {"type": "integer"}}, "required": ["node", "container_id"]}}},
    {"type": "function", "function": {"name": "systemctl_status", "description": "Get systemd unit status on a node.", "parameters": {"type": "object", "properties": {"node": {"type": "string", "enum": NODES}, "unit": {"type": "string"}}, "required": ["node", "unit"]}}},
    {"type": "function", "function": {"name": "df_path", "description": "Check disk usage for a path on a node.", "parameters": {"type": "object", "properties": {"node": {"type": "string", "enum": NODES}, "path": {"type": "string"}}, "required": ["node", "path"]}}},
    {"type": "function", "function": {"name": "ls_path", "description": "List a path on a node.", "parameters": {"type": "object", "properties": {"node": {"type": "string", "enum": NODES}, "path": {"type": "string"}}, "required": ["node", "path"]}}},
    {"type": "function", "function": {"name": "submit_diagnosis", "description": "Submit one root cause label. Required before repairs; returns an approval token if correct.", "parameters": {"type": "object", "properties": {"root_cause": {"type": "string", "enum": ROOT_CAUSE_LABELS}, "affected": {"type": "array", "items": {"type": "string", "enum": NODES}}}, "required": ["root_cause"]}}},
    {"type": "function", "function": {"name": "restart_member", "description": "Restart an etcd member on a node. Requires approval_token from submit_diagnosis.", "parameters": {"type": "object", "properties": {"node": {"type": "string", "enum": NODES}, "approval_token": {"type": "string"}}, "required": ["node", "approval_token"]}}},
    {"type": "function", "function": {"name": "member_remove_readd", "description": "Remove and re-add an etcd member on a node. Requires approval_token.", "parameters": {"type": "object", "properties": {"node": {"type": "string", "enum": NODES}, "approval_token": {"type": "string"}}, "required": ["node", "approval_token"]}}},
    {"type": "function", "function": {"name": "kube_etcd_switch_rebuild", "description": "Rebuild an etcd member via kube-etcd-switch. Requires approval_token.", "parameters": {"type": "object", "properties": {"node": {"type": "string", "enum": NODES}, "approval_token": {"type": "string"}}, "required": ["node", "approval_token"]}}},
    {"type": "function", "function": {"name": "snapshot_restore_full", "description": "Restore etcd from full snapshot. Requires approval_token.", "parameters": {"type": "object", "properties": {"approval_token": {"type": "string"}}, "required": ["approval_token"]}}},
    {"type": "function", "function": {"name": "reset_cluster", "description": "Reset the whole cluster. Requires approval_token.", "parameters": {"type": "object", "properties": {"approval_token": {"type": "string"}}, "required": ["approval_token"]}}},
]
REPAIR_TOOLS = {"restart_member", "member_remove_readd", "kube_etcd_switch_rebuild", "snapshot_restore_full", "reset_cluster"}
DIAGNOSIS_TOOL = "submit_diagnosis"
