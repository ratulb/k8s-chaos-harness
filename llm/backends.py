from __future__ import annotations

import os

from llm.client import Backend
from tools.naming import normalize_backend

BACKEND_NAMES = ("ollama", "groq", "openrouter", "cloudflare")


def build_backend(name: str) -> Backend:
    backend_name = normalize_backend(name)
    if backend_name == "ollama":
        return Backend(
            name="ollama",
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
            model=os.environ.get("OLLAMA_MODEL", "qwen3.8:27b"),
            api_key=os.environ.get("OLLAMA_API_KEY", "ollama"),
        )
    if backend_name == "groq":
        return Backend(
            name="groq",
            base_url="https://api.groq.com/openai/v1",
            model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
            api_key=os.environ.get("GROQ_API_KEY", ""),
        )
    if backend_name == "openrouter":
        return Backend(
            name="openrouter",
            base_url="https://openrouter.ai/api/v1",
            model=os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"),
            api_key=os.environ.get("OPENROUTER_API_KEY", ""),
        )
    if backend_name == "cloudflare":
        account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        return Backend(
            name="cloudflare",
            base_url=f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1",
            model=os.environ.get("CLOUDFLARE_MODEL", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"),
            api_key=os.environ.get("CLOUDFLARE_AUTH_TOKEN", ""),
        )
    raise ValueError(f"unknown backend {name!r}; expected one of {', '.join(BACKEND_NAMES)}")


def configured_backends() -> list[Backend]:
    out: list[Backend] = []
    if os.environ.get("OLLAMA_BASE_URL"):
        out.append(build_backend("ollama"))
    if os.environ.get("GROQ_API_KEY"):
        out.append(build_backend("groq"))
    if os.environ.get("OPENROUTER_API_KEY"):
        out.append(build_backend("openrouter"))
    if os.environ.get("CLOUDFLARE_ACCOUNT_ID") and os.environ.get("CLOUDFLARE_AUTH_TOKEN"):
        out.append(build_backend("cloudflare"))
    return out
