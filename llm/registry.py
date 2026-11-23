from __future__ import annotations

import json
import os
from dataclasses import replace
from typing import Any

from llm.backends import configured_backends
from llm.client import Backend
from tools.naming import LEGACY_BACKEND_ALIASES, normalize_backend

AUTO_BACKEND = "auto"


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _priority(spec: dict[str, Any], index: int) -> int:
    try:
        return int(spec.get("priority", 100 + index))
    except (TypeError, ValueError):
        return 100 + index


def _custom_backends() -> list[Backend]:
    raw = os.environ.get("CHAOS_LLM_ENDPOINTS", "").strip()
    if not raw:
        return []
    try:
        specs = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"CHAOS_LLM_ENDPOINTS must be a JSON list: {exc}") from exc
    if not isinstance(specs, list):
        raise ValueError("CHAOS_LLM_ENDPOINTS must be a JSON list")

    out: list[Backend] = []
    names: set[str] = set()
    for index, item in enumerate(specs):
        if not isinstance(item, dict):
            raise ValueError(f"CHAOS_LLM_ENDPOINTS[{index}] must be an object")
        name = str(item.get("name") or "").strip()
        base_url = str(item.get("base_url") or "").strip()
        model = str(item.get("model") or "").strip()
        if not name or not base_url or not model:
            raise ValueError(f"CHAOS_LLM_ENDPOINTS[{index}] requires name, base_url, and model")
        if name == AUTO_BACKEND or name in LEGACY_BACKEND_ALIASES or name in names:
            raise ValueError(f"CHAOS_LLM_ENDPOINTS[{index}] has reserved or duplicate name {name!r}")
        if not _env_flag(str(item.get("enabled", "1")), True):
            continue
        names.add(name)
        key_env = str(item.get("api_key_env") or "").strip()
        api_key = os.environ.get(key_env, "") if key_env else str(item.get("api_key") or "")
        if not api_key and ("127.0.0.1" in base_url or "localhost" in base_url):
            api_key = "ollama"
        out.append(Backend(
            name=name,
            base_url=base_url,
            model=model,
            api_key=api_key,
            priority=_priority(item, index),
        ))
    return out


def all_backends() -> list[Backend]:
    merged: dict[str, Backend] = {}
    for backend in configured_backends():
        merged[backend.name] = backend
    for backend in _custom_backends():
        merged[backend.name] = backend
    return sorted(merged.values(), key=lambda backend: (backend.priority, backend.name))


def backend_choices() -> list[str]:
    names = [AUTO_BACKEND]
    names.extend(backend.name for backend in all_backends())
    return names


def fallback_enabled() -> bool:
    return _env_flag("CHAOS_LLM_FALLBACK", True)


def resolve_backend(selector: str) -> Backend:
    backends = all_backends()
    if not backends:
        raise ValueError("no LLM endpoints configured; set OLLAMA_BASE_URL, a hosted API key, or CHAOS_LLM_ENDPOINTS")

    if selector == AUTO_BACKEND or not selector.strip():
        primary = backends[0]
        rest = backends[1:]
    else:
        name = normalize_backend(selector)
        matches = [backend for backend in backends if backend.name == name]
        if not matches:
            expected = ", ".join(backend.name for backend in backends)
            raise ValueError(f"unknown backend {selector!r}; expected one of {AUTO_BACKEND}, {expected}")
        primary = matches[0]
        rest = [backend for backend in backends if backend.name != name]

    if not fallback_enabled():
        rest = []
    try:
        max_attempts = int(os.environ.get("CHAOS_LLM_MAX_ATTEMPTS", "0"))
    except ValueError:
        max_attempts = 0
    if max_attempts > 0:
        rest = rest[: max(0, max_attempts - 1)]
    return replace(primary, fallbacks=tuple(rest))


__all__ = [
    "AUTO_BACKEND",
    "all_backends",
    "backend_choices",
    "fallback_enabled",
    "resolve_backend",
]
