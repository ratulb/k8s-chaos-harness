from __future__ import annotations

import subprocess
from dataclasses import dataclass

from common.results import Result
from harness.journal import Journal
from harness.spec import ExperimentSpec
from tools.truncate import truncate


@dataclass(frozen=True)
class Verification:
    ok: bool
    output: str

    @classmethod
    def run(cls, spec: ExperimentSpec) -> "Verification":
        try:
            completed = subprocess.run(
                spec.verify_cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=spec.verify_timeout,
                env=dict(spec.run_env),
            )
            ok = completed.returncode == 0
            output = (completed.stdout + completed.stderr).strip()
        except Exception as exc:
            ok = False
            output = str(exc)
        return cls(ok=ok, output=output)

    @property
    def result(self) -> Result[str]:
        return Result.success(self.output) if self.ok else Result.failure(self.output)

    def commit(self, journal: Journal) -> None:
        journal.artifact("verify_output_full", {"output": self.output})
        journal.event({"type": "verify", "ok": self.ok, "output": truncate(self.output, 2000)})

    def record_fields(self) -> dict[str, bool | str]:
        return {"verify_ok": self.ok, "verify_output": truncate(self.output, 1000)}
