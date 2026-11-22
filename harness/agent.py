from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any, Callable

from harness.engine import _redact_message, run_scenario as _run_scenario
from harness.message import Message
from harness.prompt import SystemPrompt
from harness.tooling import AgentState, Diagnosis, Repair, normalize_diagnosis
from llm.client import LLMClient
from tools.tool import TOOL_REGISTRY, ToolContext, ToolOutcome, build_tool_registry


@dataclass(frozen=True)
class Agent:
    client_factory: Callable[[Any], Any]
    tool_registry: Any
    diagnosis_class: Any
    repair_class: Any

    def run(self, *args: Any, **kwargs: Any) -> dict:
        return _run_scenario(
            *args,
            client_factory=self.client_factory,
            tool_registry=self.tool_registry,
            diagnosis_class=self.diagnosis_class,
            repair_class=self.repair_class,
            **kwargs,
        )


def build_agent() -> Agent:
    return Agent(
        client_factory=LLMClient,
        tool_registry=TOOL_REGISTRY,
        diagnosis_class=Diagnosis,
        repair_class=Repair,
    )


def run_scenario(
    scenario: dict,
    backend,
    run_id: str,
    inject_cmd: str,
    verify_cmd: str,
    trace_dir: str = "traces",
    verify_timeout: int = 300,
    baseline_ok: bool = True,
    reset_ok: bool = True,
    inject_ok: bool = True,
    fault_present: bool = True,
    setup_output: str = "",
    store: Any = None,
    resume: bool = False,
    reporter: Any = None,
    log_path: str | None = None,
    approve: str = "policy",
    pause_reporter: Any = None,
    resume_reporter: Any = None,
) -> dict:
    return build_agent().run(
        scenario,
        backend,
        run_id,
        inject_cmd,
        verify_cmd,
        trace_dir=trace_dir,
        verify_timeout=verify_timeout,
        baseline_ok=baseline_ok,
        reset_ok=reset_ok,
        inject_ok=inject_ok,
        fault_present=fault_present,
        setup_output=setup_output,
        store=store,
        resume=resume,
        reporter=reporter,
        log_path=log_path,
        approve=approve,
        pause_reporter=pause_reporter,
        resume_reporter=resume_reporter,
    )


__all__ = [
    "Agent",
    "AgentState",
    "Diagnosis",
    "LLMClient",
    "Message",
    "Repair",
    "SystemPrompt",
    "TOOL_REGISTRY",
    "ToolContext",
    "ToolOutcome",
    "_redact_message",
    "build_agent",
    "build_tool_registry",
    "normalize_diagnosis",
    "run_scenario",
    "subprocess",
]
