from __future__ import annotations

import subprocess
from dataclasses import dataclass

from common.results import Result
from harness.journal import Journal
from harness.spec import ExperimentSpec
from tools import fault_checks
from tools.truncate import truncate


@dataclass(frozen=True)
class Verification:
    ok: bool
    output: str
    fault_cleared: bool = True
    fault_detail: str = ""

    @classmethod
    def run(
        cls,
        spec: ExperimentSpec,
        scenario: object | None = None,
        targets: list[str] | None = None,
    ) -> "Verification":
        try:
            completed = subprocess.run(
                spec.verify_cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=spec.verify_timeout,
                env=dict(spec.run_env),
            )
            health_ok = completed.returncode == 0
            output = (completed.stdout + completed.stderr).strip()
        except Exception as exc:
            health_ok = False
            output = str(exc)
        fault_cleared = True
        fault_detail = ""
        if scenario is not None:
            fault_cleared, fault_detail = fault_checks.check_fault_cleared(
                scenario, targets or [], env=spec.run_env,
            )
            suffix = f"FAULT CLEARED: {fault_detail}"
            output = f"{output}\n{suffix}" if output else suffix
        return cls(
            ok=health_ok and fault_cleared,
            output=output,
            fault_cleared=fault_cleared,
            fault_detail=fault_detail,
        )

    @property
    def result(self) -> Result[str]:
        return Result.success(self.output) if self.ok else Result.failure(self.output)

    def commit(self, journal: Journal) -> None:
        journal.artifact("verify_output_full", {"output": self.output})
        journal.artifact("fault_cleared", {
            "cleared": self.fault_cleared,
            "detail": self.fault_detail,
        })
        journal.event({
            "type": "verify",
            "ok": self.ok,
            "output": truncate(self.output, 2000),
            "fault_cleared": self.fault_cleared,
            "fault_detail": truncate(self.fault_detail, 500),
        })

    def record_fields(self) -> dict[str, bool | str]:
        return {"verify_ok": self.ok, "verify_output": truncate(self.output, 1000)}
