from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from common.vocab import (
    CRICTL_LOGS,
    CRICTL_PS,
    DF_PATH,
    DESTRUCTIVE_TOOLS,
    DIAGNOSIS_TOOL,
    ETCDCTL_ALARM_LIST,
    ETCDCTL_ENDPOINT_HEALTH,
    ETCDCTL_ENDPOINT_STATUS,
    ETCDCTL_MEMBER_LIST,
    JOURNALCTL_TAIL,
    KUBECTL_DESCRIBE,
    KUBECTL_EVENTS,
    KUBECTL_GET,
    KUBECTL_LOGS,
    KUBE_ETCD_SWITCH_REBUILD,
    LS_PATH,
    MEMBER_REMOVE_READD,
    RESET_CLUSTER,
    RESTART_MEMBER,
    ROOT_CAUSE_LABELS,
    SNAPSHOT_RESTORE_FULL,
    SYSTEMCTL_STATUS,
)
from tools import cluster_config, k8s, node_etcd, repairs


@dataclass
class ToolOutcome:
    message: str
    status: str = "finished"
    approved_by: str | None = None
    call_error: str | None = None
    diagnosis_submitted: bool = False
    diagnosis: str | None = None
    diagnosis_raw: Any = None
    diagnosis_correct: bool | None = None
    approval_token: str | None = None
    approval_source: str | None = None
    repair: str | None = None
    repair_args: dict[str, Any] | None = None
    policy_violation: str | None = None
    forbidden_reason: str | None = None
    artifact_kind: str | None = None
    artifact_payload: Any = None


@dataclass
class ToolContext:
    call_id: str
    diagnosis: Any | None = None
    repair: Any | None = None


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    properties: dict[str, Any] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    handler: Callable[[dict[str, Any]], str | ToolOutcome] | None = None
    kind: str = "diagnostic"
    mutating: bool = False
    destructive: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.properties,
                    "required": list(self.required),
                },
            },
        }

    def to_string(self) -> str:
        return json.dumps(self.schema(), sort_keys=True)

    def __str__(self) -> str:
        return self.to_string()

    def invoke_handler(self, args: dict[str, Any]) -> str | ToolOutcome:
        if self.handler is None:
            return f"unknown tool: {self.name}"
        return self.handler(args)

    def run(self, args: dict[str, Any], context: ToolContext | None = None) -> ToolOutcome:
        try:
            if self.kind == "diagnosis":
                if context is None or context.diagnosis is None:
                    result = "diagnosis requires runtime context"
                else:
                    result = context.diagnosis.execute(self, args, context.call_id)
            elif self.kind == "repair":
                if context is None or context.repair is None:
                    result = self.invoke_handler(args)
                else:
                    result = context.repair.execute(self, args, context.call_id)
            else:
                result = self.invoke_handler(args)
        except Exception as e:
            result = f"ERROR: {e}"
        if isinstance(result, ToolOutcome):
            return result
        message = str(result)
        status = "failed" if message.startswith("ERROR:") else "finished"
        repair = self.name if self.kind == "repair" and status == "finished" else None
        return ToolOutcome(message=message, status=status, repair=repair)


@dataclass
class ToolRegistry:
    tools: dict[str, Tool]

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.tools.values()]

    def to_strings(self) -> list[str]:
        return [tool.to_string() for tool in self.tools.values()]

    def __iter__(self):
        return iter(self.tools.values())


def _node_and_error(value: str) -> tuple[str | None, str]:
    nodes = cluster_config.nodes()
    node = value or cluster_config.default_node()
    if node not in nodes:
        return None, f"invalid node {value!r}; expected one of {', '.join(nodes)}"
    return node, ""


def _with_node(fn: Callable[..., str], *specs: tuple[str, Any]) -> Callable[[dict[str, Any]], str]:
    def handler(args: dict[str, Any]) -> str:
        node, error = _node_and_error(str(args.get("node", "")))
        if error:
            return error
        return fn(node, *(args.get(key, default) for key, default in specs))
    return handler


def _with_default_node(fn: Callable[[], str]) -> Callable[[dict[str, Any]], str]:
    def handler(args: dict[str, Any]) -> str:
        _, error = _node_and_error("")
        return error or fn()
    return handler


def build_tool_registry() -> ToolRegistry:
    nodes = cluster_config.nodes()
    probe = cluster_config.default_node()

    def node_prop(default: str | None = None) -> dict[str, Any]:
        prop: dict[str, Any] = {"type": "string", "enum": nodes}
        if default:
            prop["default"] = default
        return prop

    tools = [
        Tool(KUBECTL_GET, "Get Kubernetes resources.", {"resource": {"type": "string"}, "namespace": {"type": "string"}}, ("resource",),
             lambda args: k8s.kubectl_get(args.get("resource", ""), args.get("namespace", ""))),
        Tool(KUBECTL_DESCRIBE, "Describe a Kubernetes resource.", {"resource": {"type": "string"}, "name": {"type": "string"}, "namespace": {"type": "string"}}, ("resource", "name"),
             lambda args: k8s.kubectl_describe(args.get("resource", ""), args.get("name", ""), args.get("namespace", ""))),
        Tool(KUBECTL_LOGS, "Get pod logs.", {"pod": {"type": "string"}, "container": {"type": "string"}, "tail_lines": {"type": "integer"}, "namespace": {"type": "string"}}, ("pod",),
             lambda args: k8s.kubectl_logs(args.get("pod", ""), args.get("container", ""), args.get("tail_lines", 100), args.get("namespace", "default"))),
        Tool(KUBECTL_EVENTS, "Get namespace events.", {"namespace": {"type": "string"}}, (),
             lambda args: k8s.kubectl_events(args.get("namespace", "default"))),
        Tool(ETCDCTL_ENDPOINT_HEALTH, "Check etcd endpoint health across the cluster.", {}, (),
             lambda args: node_etcd.etcdctl_endpoint_health()),
        Tool(ETCDCTL_ENDPOINT_STATUS, "Get etcd endpoint status from a node.", {"node": node_prop(probe)}, (),
             _with_node(node_etcd.etcdctl_endpoint_status)),
        Tool(ETCDCTL_MEMBER_LIST, "List etcd members from a node.", {"node": node_prop(probe)}, (),
             _with_node(node_etcd.etcdctl_member_list)),
        Tool(ETCDCTL_ALARM_LIST, "List etcd alarms from a node.", {"node": node_prop(probe)}, (),
             _with_node(node_etcd.etcdctl_alarm_list)),
        Tool(JOURNALCTL_TAIL, "Tail a systemd unit journal on a node.", {"node": node_prop(), "unit": {"type": "string"}, "lines": {"type": "integer"}}, ("node", "unit"),
             _with_node(node_etcd.journalctl_tail, ("unit", ""), ("lines", 100))),
        Tool(CRICTL_PS, "List containers on a node.", {"node": node_prop()}, ("node",),
             _with_node(node_etcd.crictl_ps)),
        Tool(CRICTL_LOGS, "Get container logs on a node.", {"node": node_prop(), "container_id": {"type": "string"}, "tail": {"type": "integer"}}, ("node", "container_id"),
             _with_node(node_etcd.crictl_logs, ("container_id", ""), ("tail", 100))),
        Tool(SYSTEMCTL_STATUS, "Get systemd unit status on a node.", {"node": node_prop(), "unit": {"type": "string"}}, ("node", "unit"),
             _with_node(node_etcd.systemctl_status, ("unit", ""))),
        Tool(DF_PATH, "Check disk usage for a path on a node.", {"node": node_prop(), "path": {"type": "string"}}, ("node", "path"),
             _with_node(node_etcd.df_path, ("path", ""))),
        Tool(LS_PATH, "List a path on a node.", {"node": node_prop(), "path": {"type": "string"}}, ("node", "path"),
             _with_node(node_etcd.ls_path, ("path", ""))),
        Tool(DIAGNOSIS_TOOL, "Submit one root cause label. Required before repairs; returns an approval token if correct.",
             {"root_cause": {"type": "string", "enum": ROOT_CAUSE_LABELS}, "affected": {"type": "array", "items": {"type": "string", "enum": nodes}}},
             ("root_cause",), kind="diagnosis"),
        Tool(RESTART_MEMBER, "Restart an etcd member on a node. Requires approval_token from submit_diagnosis.",
             {"node": node_prop(), "approval_token": {"type": "string"}}, ("node", "approval_token"),
             _with_node(repairs.restart_member), kind="repair", mutating=True),
        Tool(MEMBER_REMOVE_READD, "Remove and re-add an etcd member on a node. Requires approval_token.",
             {"node": node_prop(), "approval_token": {"type": "string"}}, ("node", "approval_token"),
             _with_node(repairs.member_remove_readd), kind="repair", mutating=True),
        Tool(KUBE_ETCD_SWITCH_REBUILD, "Rebuild an etcd member via kube-etcd-switch. Requires approval_token.",
             {"node": node_prop(), "approval_token": {"type": "string"}}, ("node", "approval_token"),
             _with_node(repairs.kube_etcd_switch_rebuild), kind="repair", mutating=True),
        Tool(SNAPSHOT_RESTORE_FULL, "Restore etcd from full snapshot. Requires approval_token.",
             {"approval_token": {"type": "string"}}, ("approval_token",),
             _with_default_node(repairs.snapshot_restore_full), kind="repair", mutating=True),
        Tool(RESET_CLUSTER, "Reset the whole cluster. Requires approval_token.",
             {"approval_token": {"type": "string"}}, ("approval_token",),
             _with_default_node(repairs.reset_cluster), kind="repair", mutating=True),
    ]
    tools = [replace(tool, destructive=tool.name in DESTRUCTIVE_TOOLS) if tool.mutating else tool for tool in tools]
    return ToolRegistry({tool.name: tool for tool in tools})
