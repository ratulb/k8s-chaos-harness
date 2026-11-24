from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Callable

from harness.loop import AgentLoop, RunResult
from harness.spec import ExperimentSpec, RunContext
from harness.tooling import Diagnosis, Repair
from llm.client import LLMClient
from tools.scenario import as_scenario
from tools.tool import TOOL_REGISTRY


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
    client_factory: Callable[[Any], Any] | None = None,
    tool_registry: Any = None,
    diagnosis_class: Any = Diagnosis,
    repair_class: Any = Repair,
    run_env: dict[str, str] | None = None,
) -> dict:
    temp: tempfile.TemporaryDirectory[str] | None = None
    if store is None:
        from storage.sqlite_store import SQLiteStore

        temp = tempfile.TemporaryDirectory()
        store = SQLiteStore(Path(temp.name) / "agent.sqlite3")
        if not store.run_exists(run_id):
            value = as_scenario(scenario)
            store.create_run(
                run_id,
                scenario_id=value.id,
                backend=getattr(backend, "name", "unknown"),
                truth=value.root_cause,
                interactive=approve == "prompt",
            )
    try:
        spec = ExperimentSpec(
            scenario=as_scenario(scenario),
            backend=backend,
            inject_cmd=inject_cmd,
            verify_cmd=verify_cmd,
            verify_timeout=verify_timeout,
            trace_dir=trace_dir,
            run_env=dict(run_env or {}),
            approve=approve,
        )
        ctx = RunContext(
            run_id=run_id,
            store=store,
            reporter=reporter,
            resume=resume,
            log_path=log_path,
            setup_output=setup_output,
            baseline_ok=baseline_ok,
            reset_ok=reset_ok,
            inject_ok=inject_ok,
            fault_present=fault_present,
            pause_reporter=pause_reporter,
            resume_reporter=resume_reporter,
        )
        loop = AgentLoop(
            client_factory=client_factory or LLMClient,
            tool_registry=tool_registry or TOOL_REGISTRY,
            diagnosis_class=diagnosis_class,
            repair_class=repair_class,
        )
        result: RunResult = loop.run(spec, ctx)
        return result.to_record()
    finally:
        if temp is not None:
            store.close()
            temp.cleanup()
