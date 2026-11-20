#!/usr/bin/env python3
"""Check configured LLM backends."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm.client import Backend, LLMClient
from llm.fallback import ready_backends

backends: list[Backend] = []

ollama_url = os.environ.get("OLLAMA_BASE_URL")
if ollama_url:
    backends.append(
        Backend(
            name="kaggle-ollama",
            base_url=ollama_url,
            model=os.environ.get("OLLAMA_MODEL", "qwen3.8:27b"),
            api_key=os.environ.get("OLLAMA_API_KEY", "ollama"),
        )
    )

groq_key = os.environ.get("GROQ_API_KEY")
if groq_key:
    backends.append(
        Backend(
            name="groq",
            base_url="https://api.groq.com/openai/v1",
            model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
            api_key=groq_key,
        )
    )

openrouter_key = os.environ.get("OPENROUTER_API_KEY")
if openrouter_key:
    backends.append(
        Backend(
            name="openrouter",
            base_url="https://openrouter.ai/api/v1",
            model=os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"),
            api_key=openrouter_key,
        )
    )

cf_account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
cf_token = os.environ.get("CLOUDFLARE_AUTH_TOKEN")
if cf_account and cf_token:
    backends.append(
        Backend(
            name="cloudflare",
            base_url=f"https://api.cloudflare.com/client/v4/accounts/{cf_account}/ai/v1",
            model=os.environ.get("CLOUDFLARE_MODEL", "@cf/qwen/qwq-32b"),
            api_key=cf_token,
        )
    )

if not backends:
    print("No backends configured. Set OLLAMA_BASE_URL or hosted API keys.")
    raise SystemExit(1)

for backend in backends:
    status = "READY" if LLMClient(backend).health() else "DOWN"
    print(f"{backend.name}: {status} ({backend.base_url})")

ready = ready_backends(backends)
print(f"\nReady backends: {len(ready)}/{len(backends)}")
if not ready:
    raise SystemExit(1)
