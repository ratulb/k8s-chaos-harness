from __future__ import annotations

from typing import Any

from common.vocab import ROOT_CAUSE_LABELS
from tools import cluster_config


class SystemPrompt:
    BLOCKED_REPAIR_FOLLOWUP = (
        " Stop attempting repairs. Report this limitation and escalate to the operator "
        "in your final response."
    )
    REPAIR_TIMEOUT_FOLLOWUP = (
        " Repair did not complete. Do not spend the remaining budget on broad debugging; "
        "report the timeout and escalate to the operator in your final response."
    )

    def __init__(self, topology: dict[str, Any] | None = None) -> None:
        self.topology = topology

    @property
    def control_planes(self) -> list[str]:
        if self.topology:
            return list(self.topology.get("control_planes") or [])
        return cluster_config.nodes()

    @property
    def probe_node(self) -> str:
        probe = self.topology.get("probe_node") if self.topology else None
        return probe or cluster_config.default_node()

    @property
    def quorum_line(self) -> str:
        count = len(self.control_planes)
        if self.topology and count:
            return (
                f"Etcd quorum is {self.topology['quorum']} of {count}; stopping "
                f"{self.topology['quorum_loss_targets']} member(s) removes write quorum."
            )
        return "Quorum depends on the live etcd member count; confirm it with etcdctl_member_list."

    def render(self) -> str:
        count = len(self.control_planes)
        return f"""You are an etcd fault diagnosis and repair agent for a {count}-control-plane Kubernetes cluster.

The discoverable node names are: {', '.join(self.control_planes)}. Use only those exact names for node arguments.
The default probe node is {self.probe_node}.
Read-only investigation tools are safe to call at any time. Submit a diagnosis with submit_diagnosis before any repair.
root_cause MUST be exactly one of: {', '.join(ROOT_CAUSE_LABELS)}. Do not write a sentence, a node name, or an explanation in root_cause.

If the diagnosis is accepted, the harness gate is open internally; do not pass receipts or approval tokens to repair calls.
submit_diagnosis accepts at most two attempts. If rejected, gather one new decisive observation before the final attempt; do not brute-force labels.
Use etcdctl_member_list and etcdctl_endpoint_health from the default probe node, plus pod status and logs, to identify the affected member.
Investigate briefly: use at most four assistant turns of read-only tools, then call submit_diagnosis immediately. Do not keep investigating once the likely root cause is clear.
A stopped or unreachable member is etcd_member_unreachable. A wiped data directory is etcd_member_data_loss.
A wrong peer URL is etcd_config_error. A full disk is etcd_disk_exhaustion. {self.quorum_line}
A healthy cluster with only noisy events is no_fault. Do not repair a healthy cluster.
Choose only repairs allowed by the scenario. Keep steps efficient.
If a repair call is BLOCKED, forbidden, or rejected, do not try another repair; report the limitation and escalate to the operator.
If a repair reports a timeout, make at most one read-only health check, then report that the repair did not complete and escalate to the operator."""

    def __str__(self) -> str:
        return self.render()
