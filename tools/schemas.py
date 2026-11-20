from __future__ import annotations

TOOLS = [
    {"type": "function", "function": {"name": "kubectl_get", "description": "Get Kubernetes resources.", "parameters": {"type": "object", "properties": {"resource": {"type": "string"}, "namespace": {"type": "string"}}, "required": ["resource"]}}},
    {"type": "function", "function": {"name": "kubectl_describe", "description": "Describe a Kubernetes resource.", "parameters": {"type": "object", "properties": {"resource": {"type": "string"}, "name": {"type": "string"}, "namespace": {"type": "string"}}, "required": ["resource", "name"]}}},
    {"type": "function", "function": {"name": "kubectl_logs", "description": "Get pod logs.", "parameters": {"type": "object", "properties": {"pod": {"type": "string"}, "container": {"type": "string"}, "tail_lines": {"type": "integer"}, "namespace": {"type": "string"}}, "required": ["pod"]}}},
    {"type": "function", "function": {"name": "kubectl_events", "description": "Get namespace events.", "parameters": {"type": "object", "properties": {"namespace": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {"name": "etcdctl_endpoint_health", "description": "Check etcd endpoint health.", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "etcdctl_endpoint_status", "description": "Get etcd endpoint status.", "parameters": {"type": "object", "properties": {"node": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {"name": "etcdctl_member_list", "description": "List etcd members.", "parameters": {"type": "object", "properties": {"node": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {"name": "etcdctl_alarm_list", "description": "List etcd alarms.", "parameters": {"type": "object", "properties": {"node": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {"name": "journalctl_tail", "description": "Tail a systemd unit journal on a node.", "parameters": {"type": "object", "properties": {"node": {"type": "string"}, "unit": {"type": "string"}, "lines": {"type": "integer"}}, "required": ["node", "unit"]}}},
    {"type": "function", "function": {"name": "crictl_ps", "description": "List containers on a node.", "parameters": {"type": "object", "properties": {"node": {"type": "string"}}, "required": ["node"]}}},
    {"type": "function", "function": {"name": "crictl_logs", "description": "Get container logs on a node.", "parameters": {"type": "object", "properties": {"node": {"type": "string"}, "container_id": {"type": "string"}, "tail": {"type": "integer"}}, "required": ["node", "container_id"]}}},
    {"type": "function", "function": {"name": "systemctl_status", "description": "Get systemd unit status on a node.", "parameters": {"type": "object", "properties": {"node": {"type": "string"}, "unit": {"type": "string"}}, "required": ["node", "unit"]}}},
    {"type": "function", "function": {"name": "df_path", "description": "Check disk usage for a path on a node.", "parameters": {"type": "object", "properties": {"node": {"type": "string"}, "path": {"type": "string"}}, "required": ["node", "path"]}}},
    {"type": "function", "function": {"name": "ls_path", "description": "List a path on a node.", "parameters": {"type": "object", "properties": {"node": {"type": "string"}, "path": {"type": "string"}}, "required": ["node", "path"]}}},
    {"type": "function", "function": {"name": "submit_diagnosis", "description": "Submit root cause diagnosis. Required before repairs.", "parameters": {"type": "object", "properties": {"root_cause": {"type": "string"}, "affected": {"type": "array", "items": {"type": "string"}}}, "required": ["root_cause"]}}},
    {"type": "function", "function": {"name": "restart_member", "description": "Restart an etcd member on a node.", "parameters": {"type": "object", "properties": {"node": {"type": "string"}}, "required": ["node"]}}},
    {"type": "function", "function": {"name": "member_remove_readd", "description": "Remove and re-add an etcd member on a node.", "parameters": {"type": "object", "properties": {"node": {"type": "string"}}, "required": ["node"]}}},
    {"type": "function", "function": {"name": "kube_etcd_switch_rebuild", "description": "Rebuild an etcd member via kube-etcd-switch.", "parameters": {"type": "object", "properties": {"node": {"type": "string"}}, "required": ["node"]}}},
    {"type": "function", "function": {"name": "snapshot_restore_full", "description": "Restore etcd from full snapshot.", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "reset_cluster", "description": "Reset the whole cluster.", "parameters": {"type": "object", "properties": {}, "required": []}}},
]
REPAIR_TOOLS = {"restart_member", "member_remove_readd", "kube_etcd_switch_rebuild", "snapshot_restore_full", "reset_cluster"}
DIAGNOSIS_TOOL = "submit_diagnosis"
