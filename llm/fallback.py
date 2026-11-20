"""Try configured backends in order."""

from __future__ import annotations

from llm.client import Backend, LLMClient


def ready_backends(backends: list[Backend]) -> list[Backend]:
    ready = []
    for backend in backends:
        if LLMClient(backend).health():
            ready.append(backend)
    return ready


def first_ready(backends: list[Backend]) -> Backend | None:
    ready = ready_backends(backends)
    return ready[0] if ready else None
