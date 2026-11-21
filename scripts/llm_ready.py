#!/usr/bin/env python3
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from llm.env import load_env

load_env()

from llm.backends import configured_backends
from llm.client import LLMClient
from llm.fallback import ready_backends

backends = configured_backends()

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
