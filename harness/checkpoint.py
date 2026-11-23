from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from harness.budget import Budget
from harness.ports import RunStore
from harness.run_runtime import RunRuntime
from harness.tooling import AgentState
from tools.scenario import Scenario


@dataclass
class LeaseRenewer:
    renew: Callable[[], None]
    interval: float = 60.0
    _stop: threading.Event = field(init=False, repr=False)
    _thread: threading.Thread | None = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        self._stop = threading.Event()

    def __enter__(self) -> "LeaseRenewer":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.renew()
            except Exception:
                pass


@dataclass
class Checkpoint:
    run_id: str
    store: RunStore
    state: AgentState
    run: RunRuntime
    scenario: Scenario
    budget: Budget
    trace_path: Path | str

    def __post_init__(self) -> None:
        self.trace_path = Path(self.trace_path)

    def capture(self) -> dict[str, Any]:
        snapshot = self.state.snapshot(
            trace_path=str(self.trace_path),
            seq=int(self.store.get_run(self.run_id)["seq"]),
            steps=self.budget.steps,
            tokens_used=self.budget.tokens_used,
            diagnosis_nudged=self.budget.diagnosis_nudged,
        )
        snapshot["run"] = self.run.to_snapshot()
        self.store.set_state(self.run_id, snapshot)
        if self.run.renew_lease:
            self.store.renew_lease(self.run_id)
        self.store.update_run(self.run_id, **self.projection())
        return snapshot

    def projection(self) -> dict[str, Any]:
        run_projection = self.run.projection()
        run_projection.pop("end_reason", None)
        return {
            **run_projection,
            "diagnosis": self.state.diagnosis,
            "diagnosis_raw": self.state.diagnosis_raw,
            "diagnosis_correct": self.state.diagnosis_correct if self.state.diagnosis_submitted else None,
            "approval_granted": self.state.receipt is not None,
            "repair": self.state.repair,
            "repair_args_json": self.state.repair_args,
            "policy_violations_json": self.state.policy_violations,
            "forbidden_reason": self.state.forbidden_reason,
            "steps": self.budget.steps,
            "tokens_used": self.budget.tokens_used,
            "trace_path": str(self.trace_path),
        }
