from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol, Sequence

if TYPE_CHECKING:
    from llm.response import LLMResponse


class RunStore(Protocol):
    def add_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> int: ...

    def add_artifact(
        self,
        run_id: str,
        kind: str,
        content: str | bytes,
        content_type: str = "text/plain",
    ) -> int: ...

    def get_state(self, run_id: str) -> dict[str, Any]: ...

    def set_state(self, run_id: str, state: dict[str, Any], source: str = "checkpoint") -> int: ...

    def load_messages(self, run_id: str) -> list[dict[str, Any]]: ...

    def append_message(
        self,
        run_id: str,
        role: str,
        content: str | None,
        tool_calls: Sequence[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        tokens: int | None = None,
        backend: str | None = None,
    ) -> int: ...

    def get_run(self, run_id: str) -> dict[str, Any]: ...

    def update_run(self, run_id: str, **fields: Any) -> None: ...

    def renew_lease(self, run_id: str) -> bool: ...

    def add_llm_request(
        self,
        run_id: str,
        backend: str,
        latency_ms: int,
        tokens_in: int,
        tokens_out: int,
        error: str | None = None,
        attempt: int = 1,
    ) -> int: ...

    def begin_tool_call(
        self,
        run_id: str,
        call_id: str,
        msg_seq: int,
        name: str,
        args: dict[str, Any],
        mutating: bool = False,
        destructive: bool = False,
        phase: str = "intended",
        status: str = "proposed",
        result_code: str | None = None,
        reason: str | None = None,
        violation_kind: str | None = None,
    ) -> int: ...

    def update_tool_call_status(
        self,
        run_id: str,
        call_id: str,
        status: str,
        phase: str | None = None,
        result_code: str | None = None,
    ) -> None: ...

    def finish_tool_call(
        self,
        run_id: str,
        call_id: str,
        status: str,
        result_raw: str,
        result_sent: str,
        truncated: bool,
        approved_by: str | None = None,
        error: str | None = None,
        phase: str | None = None,
        result_code: str | None = None,
        reason: str | None = None,
        violation_kind: str | None = None,
    ) -> None: ...

    def add_diagnosis_attempt(self, run_id: str, raw: Any, diagnosis: str | None, correct: bool) -> int: ...

    def add_policy_violation(self, run_id: str, tool: str, reason: str) -> int: ...

    def finish_run(
        self,
        run_id: str,
        record: dict[str, Any] | None = None,
        status: str = "finished",
        end_reason: str | None = None,
        state: dict[str, Any] | None = None,
        score: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def export_trace(self, run_id: str, trace_dir: str | Path) -> Path: ...


class ChatClient(Protocol):
    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        on_chunk: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> "LLMResponse | dict[str, Any]": ...
