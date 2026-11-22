from __future__ import annotations

DIAGNOSIS_TOOL = "submit_diagnosis"

REPAIR_TOOLS = frozenset({
    "restart_member",
    "member_remove_readd",
    "kube_etcd_switch_rebuild",
    "snapshot_restore_full",
    "reset_cluster",
})

DESTRUCTIVE_TOOLS = frozenset({
    "member_remove_readd",
    "kube_etcd_switch_rebuild",
    "snapshot_restore_full",
    "reset_cluster",
})

ROOT_CAUSE_LABELS = [
    "no_fault",
    "etcd_member_unreachable",
    "etcd_member_data_loss",
    "etcd_quorum_loss",
    "etcd_disk_exhaustion",
    "etcd_config_error",
]
