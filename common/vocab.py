from __future__ import annotations

KUBECTL_GET = "kubectl_get"
KUBECTL_DESCRIBE = "kubectl_describe"
KUBECTL_LOGS = "kubectl_logs"
KUBECTL_EVENTS = "kubectl_events"
ETCDCTL_ENDPOINT_HEALTH = "etcdctl_endpoint_health"
ETCDCTL_ENDPOINT_STATUS = "etcdctl_endpoint_status"
ETCDCTL_MEMBER_LIST = "etcdctl_member_list"
ETCDCTL_ALARM_LIST = "etcdctl_alarm_list"
JOURNALCTL_TAIL = "journalctl_tail"
CRICTL_PS = "crictl_ps"
CRICTL_LOGS = "crictl_logs"
SYSTEMCTL_STATUS = "systemctl_status"
DF_PATH = "df_path"
LS_PATH = "ls_path"

DIAGNOSIS_TOOL = "submit_diagnosis"

RESTART_MEMBER = "restart_member"
MEMBER_REMOVE_READD = "member_remove_readd"
KUBE_ETCD_SWITCH_REBUILD = "kube_etcd_switch_rebuild"
SNAPSHOT_RESTORE_FULL = "snapshot_restore_full"
RESET_CLUSTER = "reset_cluster"
FULL_CLUSTER_RESET = "full_cluster_reset"

REPAIR_TOOLS = frozenset({
    RESTART_MEMBER,
    MEMBER_REMOVE_READD,
    KUBE_ETCD_SWITCH_REBUILD,
    SNAPSHOT_RESTORE_FULL,
    RESET_CLUSTER,
    FULL_CLUSTER_RESET,
})

DESTRUCTIVE_TOOLS = frozenset({
    MEMBER_REMOVE_READD,
    KUBE_ETCD_SWITCH_REBUILD,
    SNAPSHOT_RESTORE_FULL,
    RESET_CLUSTER,
    FULL_CLUSTER_RESET,
})

ROOT_CAUSE_LABELS = [
    "no_fault",
    "etcd_member_unreachable",
    "etcd_member_data_loss",
    "etcd_quorum_loss",
    "etcd_disk_exhaustion",
    "etcd_config_error",
]
