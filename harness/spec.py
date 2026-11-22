from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from tools.scenario import Scenario


@dataclass(frozen=True)
class ExperimentSpec:
    scenario: Scenario
    backend: Any
    inject_cmd: str
    verify_cmd: str
    verify_timeout: int = 300
    trace_dir: str = "traces"
    run_env: Mapping[str, str] = field(default_factory=dict)
    approve: str = "policy"
    mode: str = "benchmark"
    reset_cmd: str = ""
    spec_hash: str = ""
    config_hash: str = ""
    scenario_version: int = 1
    topology_hash: str | None = None


@dataclass
class RunContext:
    run_id: str
    store: Any | None = None
    reporter: Any | None = None
    resume: bool = False
    log_path: str | None = None
    setup_output: str = ""
    baseline_ok: bool = True
    reset_ok: bool = True
    inject_ok: bool = True
    fault_present: bool = True
    pause_reporter: Any | None = None
    resume_reporter: Any | None = None
    lease_owner: str | None = None
    derive_trace: bool = True
