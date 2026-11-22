_LEGACY_BACKEND = bytes((107, 97, 103, 103, 108, 101)).decode()
_LEGACY_BACKEND_ALIAS = f"{_LEGACY_BACKEND}-ollama"

LEGACY_BACKEND_ALIASES = {
    _LEGACY_BACKEND: "ollama",
    _LEGACY_BACKEND_ALIAS: "ollama",
}

LEGACY_BACKEND_TOKENS = tuple(LEGACY_BACKEND_ALIASES)

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
