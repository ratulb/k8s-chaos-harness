from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class ResultKind(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    APPROVAL = "approval"
    REFUSAL = "refusal"


@dataclass(frozen=True)
class Result(Generic[T]):
    kind: ResultKind
    value: T | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.kind in {ResultKind.SUCCESS, ResultKind.APPROVAL}

    @classmethod
    def success(cls, value: T) -> "Result[T]":
        return cls(ResultKind.SUCCESS, value=value)

    @classmethod
    def failure(cls, error: str) -> "Result[T]":
        return cls(ResultKind.FAILURE, error=error)

    @classmethod
    def approval(cls, value: T) -> "Result[T]":
        return cls(ResultKind.APPROVAL, value=value)

    @classmethod
    def refusal(cls, value: T) -> "Result[T]":
        return cls(ResultKind.REFUSAL, value=value)

    def unwrap(self) -> T:
        if self.value is None:
            raise ValueError(self.error or f"no value for {self.kind.value}")
        return self.value


@dataclass(frozen=True)
class Approval:
    run_id: str
    diagnosis: str | None
    source: str
    message: str
    raw: Any = None
    attempt: int = 0
    diagnosis_correct: bool = False
    first_attempt_correct: bool = False


@dataclass(frozen=True)
class Refusal:
    reason: str
    message: str
    canonical: str = ""
    kind: str = "policy"
    diagnosis: str | None = None
    diagnosis_correct: bool = False
    first_attempt_correct: bool = False
    attempt: int = 0
