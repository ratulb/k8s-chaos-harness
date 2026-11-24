from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable

from harness import engine
from harness.loop import AgentLoop, RunResult
from harness.message import Message
from harness.prompt import SystemPrompt
from harness.spec import ExperimentSpec, RunContext
from harness.tooling import AgentState, Diagnosis, Repair, normalize_diagnosis
from llm.client import LLMClient
from tools.tool import TOOL_REGISTRY, ToolContext, ToolOutcome, build_tool_registry


@dataclass(frozen=True)
class Agent:
    client_factory: Callable[[Any], Any] = field(default_factory=lambda: LLMClient)
    tool_registry: Any = field(default_factory=lambda: TOOL_REGISTRY)
    diagnosis_class: Any = field(default_factory=lambda: Diagnosis)
    repair_class: Any = field(default_factory=lambda: Repair)

    def run(self, spec: ExperimentSpec, ctx: RunContext) -> RunResult:
        loop = AgentLoop(
            client_factory=self.client_factory,
            tool_registry=self.tool_registry,
            diagnosis_class=self.diagnosis_class,
            repair_class=self.repair_class,
        )
        return loop.run(spec, ctx)


def build_agent() -> Agent:
    return Agent()


def run_scenario(
    scenario: Any,
    backend: Any,
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
    run_env: dict[str, str] | None = None,
    verbose: bool = False,
) -> dict:
    agent = Agent()
    return engine.run_scenario(
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
        run_env=run_env,
        verbose=verbose,
        client_factory=agent.client_factory,
        tool_registry=agent.tool_registry,
        diagnosis_class=agent.diagnosis_class,
        repair_class=agent.repair_class,
    )


__all__ = [
    "Agent",
    "AgentLoop",
    "AgentState",
    "Diagnosis",
    "ExperimentSpec",
    "LLMClient",
    "Message",
    "Repair",
    "RunContext",
    "RunResult",
    "SystemPrompt",
    "TOOL_REGISTRY",
    "ToolContext",
    "ToolOutcome",
    "build_agent",
    "build_tool_registry",
    "normalize_diagnosis",
    "run_scenario",
    "subprocess",
]
