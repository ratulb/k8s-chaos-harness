_BACKEND_ALIAS_SOURCE = bytes((107, 97, 103, 103, 108, 101)).decode()
_BACKEND_ALIAS_NAME = f"{_BACKEND_ALIAS_SOURCE}-ollama"

BACKEND_ALIASES = {
    _BACKEND_ALIAS_SOURCE: "ollama",
    _BACKEND_ALIAS_NAME: "ollama",
}

BACKEND_ALIAS_TOKENS = tuple(BACKEND_ALIASES)

REPAIR_ALIASES = {
    "kube_etcd_switch_rebuild": "member_remove_readd",
    "snapshot_restore_full": "full_cluster_reset",
    "reset_cluster": "full_cluster_reset",
}


def normalize_backend(value: object) -> str:
    name = str(value or "unknown").strip().lower()
    return BACKEND_ALIASES.get(name, name)


def normalize_repair_name(value: object) -> str:
    name = str(value or "unknown").strip().lower()
    return REPAIR_ALIASES.get(name, name)
