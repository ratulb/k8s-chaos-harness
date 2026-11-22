LEGACY_BACKEND_ALIASES = {
    "kaggle": "ollama",
    "kaggle-ollama": "ollama",
}

REPAIR_ALIASES = {
    "kube_etcd_switch_rebuild": "member_remove_readd",
    "snapshot_restore_full": "full_cluster_reset",
    "reset_cluster": "full_cluster_reset",
}


def normalize_backend(value: object) -> str:
    name = str(value or "unknown").strip().lower()
    return LEGACY_BACKEND_ALIASES.get(name, name)


def normalize_repair_name(value: object) -> str:
    name = str(value or "unknown").strip().lower()
    return REPAIR_ALIASES.get(name, name)
