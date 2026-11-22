"""Tool schemas advertised to the LLM.

Node names are discovered at runtime so the harness works on clusters with
arbitrary hostnames.
"""
from __future__ import annotations

from common.vocab import DIAGNOSIS_TOOL, REPAIR_TOOLS, ROOT_CAUSE_LABELS
from tools.tool import ToolRegistry, build_tool_registry

from . import cluster_config


def node_enum() -> list[str]:
    return cluster_config.nodes()


def build_tools() -> list[dict]:
    return build_tool_registry().schemas()


__all__ = [
    "DIAGNOSIS_TOOL",
    "REPAIR_TOOLS",
    "ROOT_CAUSE_LABELS",
    "ToolRegistry",
    "build_tool_registry",
    "build_tools",
    "node_enum",
]
