LEGACY_BACKEND_ALIASES = {
    "kaggle": "ollama",
    "kaggle-ollama": "ollama",
}


def normalize_backend(value: object) -> str:
    name = str(value or "unknown").strip().lower()
    return LEGACY_BACKEND_ALIASES.get(name, name)
