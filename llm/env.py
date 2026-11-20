"""Load simple KEY=VALUE pairs from a .env file."""

from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | None = None) -> None:
    if path is None:
        path = os.path.join(os.getcwd(), ".env")
    env_path = Path(path)
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())
