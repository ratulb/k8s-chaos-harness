#!/usr/bin/env python3
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from llm.env import load_env

load_env()

from llm.client import LLMClient
from llm.registry import all_backends

backends = all_backends()

if not backends:
    print("No backends configured. Set OLLAMA_BASE_URL, a hosted API key, or CHAOS_LLM_ENDPOINTS.")
    raise SystemExit(1)

ready = []
for backend in backends:
    status = LLMClient(backend).health()
    if status:
        ready.append(backend)
    print(f"{backend.name}: {'READY' if status else 'DOWN'} ({backend.base_url})")

print(f"\nReady backends: {len(ready)}/{len(backends)}")
if not ready:
    raise SystemExit(1)
