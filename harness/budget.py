from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tools.scenario import Scenario


@dataclass
class Budget:
    max_steps: int
    max_tokens: int
    diagnosis_deadline_steps: int
    steps: int = 0
    tokens_used: int = 0
    diagnosis_nudged: bool = False

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any], scenario: Scenario) -> "Budget":
        return cls(
            max_steps=scenario.step_limit,
            max_tokens=scenario.token_limit,
            diagnosis_deadline_steps=scenario.diagnosis_deadline_steps,
            steps=int(snapshot.get("steps", 0)),
            tokens_used=int(snapshot.get("tokens_used", 0)),
            diagnosis_nudged=bool(snapshot.get("diagnosis_nudged", False)),
        )

    @property
    def exhausted(self) -> bool:
        return self.steps >= self.max_steps or self.tokens_used >= self.max_tokens

    def start_step(self) -> None:
        self.steps += 1

    def add_tokens(self, tokens: int) -> None:
        self.tokens_used += max(0, int(tokens))

    def exhausted_reason(self) -> str | None:
        if self.steps >= self.max_steps:
            return "budget_steps"
        if self.tokens_used >= self.max_tokens:
            return "budget_tokens"
        return None

    def should_nudge_diagnosis(self, submitted: bool, deadline: int | None = None) -> bool:
        limit = self.diagnosis_deadline_steps if deadline is None else deadline
        return not submitted and not self.diagnosis_nudged and self.steps >= limit

    def can_continue_without_tool_call(self) -> bool:
        return self.steps < self.max_steps
