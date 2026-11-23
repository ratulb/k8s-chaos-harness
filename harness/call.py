from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any

from common.results import Result, ResultKind
from harness.journal import Journal
from harness.message import Message
from tools.tool import Tool, ToolContext, ToolOutcome


@dataclass(frozen=True)
class Call:
    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    msg_seq: int = 0
    mutating: bool = False
    destructive: bool = False
    kind: str = ""

    @classmethod
    def from_tool_call(cls, payload: dict[str, Any], msg_seq: int) -> "Call":
        function = payload.get("function") or {}
        raw_args = function.get("arguments") or "{}"
        try:
            args = json.loads(raw_args)
        except (TypeError, ValueError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        return cls(
            id=str(payload.get("id") or "call"),
            name=str(function.get("name") or ""),
            args=args,
            msg_seq=msg_seq,
        )

    def for_tool(self, tool: Tool | None) -> "Call":
        return replace(
            self,
            mutating=bool(tool and tool.mutating),
            destructive=bool(tool and tool.destructive),
            kind=tool.kind if tool else "",
        )

    def invoke(self, tool: Tool | None, context: ToolContext, journal: Journal) -> "CallExecution":
        ledger = context.tool_ledger
        runtime = None
        if ledger is not None:
            runtime = ledger.for_call(self.id, self.name, self.mutating, self.destructive)
        journal.store.begin_tool_call(
            journal.run_id,
            self.id,
            self.msg_seq,
            self.name,
            Message.redact_payload(self.args),
            self.mutating,
            self.destructive,
            phase=runtime.phase.value if runtime else "intended",
            status=runtime.status if runtime else "proposed",
        )
        if tool is None:
            if runtime is not None:
                runtime.start()
                outcome = _unknown_tool(self.name)
                runtime.apply_outcome(outcome)
                outcome = replace(outcome, status=runtime.status, result_code=runtime.result_code)
            else:
                outcome = _unknown_tool(self.name)
            return CallExecution.from_outcome(
                self, self.kind, outcome,
                failed=runtime.failed if runtime else None,
                tool_state=runtime.state if runtime else None,
            )
        if runtime is not None and tool.kind not in {"diagnosis", "repair"}:
            runtime.start()
        elif tool.kind == "diagnostic" and runtime is None:
            journal.store.update_tool_call_status(journal.run_id, self.id, "started", "started")
        context.call_id = self.id
        context.call = self
        try:
            outcome = tool.run(self.args, context)
        except Exception as exc:
            outcome = ToolOutcome(message=f"ERROR: {exc}", status="failed", result_code="failed", call_error=str(exc))
        if runtime is not None and not runtime.terminal:
            runtime.apply_outcome(outcome)
            outcome = replace(outcome, status=runtime.status, result_code=runtime.result_code)
        return CallExecution.from_outcome(
            self, self.kind, outcome,
            failed=runtime.failed if runtime else None,
            tool_state=runtime.state if runtime else None,
        )


@dataclass(frozen=True)
class CallExecution:
    call: Call
    tool_kind: str
    outcome: ToolOutcome
    result: Result[ToolOutcome]
    tool_state: Any | None = None

    @classmethod
    def from_outcome(
        cls,
        call: Call,
        tool_kind: str,
        outcome: ToolOutcome,
        failed: bool | None = None,
        tool_state: Any | None = None,
    ) -> "CallExecution":
        failed = (
            bool(failed)
            if failed is not None
            else outcome.status in {"failed", "denied"} or bool(outcome.call_error)
        )
        if failed and outcome.call_error is None:
            outcome = replace(outcome, call_error=outcome.message)
        result = (
            Result.failure(outcome.call_error or outcome.message)
            if failed
            else Result.success(outcome)
        )
        return cls(call=call, tool_kind=tool_kind, outcome=outcome, result=result, tool_state=tool_state)

    @property
    def failed(self) -> bool:
        return self.result.kind is ResultKind.FAILURE

    def commit(self, journal: Journal, backend: str) -> Message:
        raw_result = self.outcome.message
        tool_message = Message.from_tool_result(raw_result, self.call.id, limit=4000)
        sent_result = str(tool_message.content or "")
        if self.outcome.artifact_kind:
            journal.artifact(self.outcome.artifact_kind, self.outcome.artifact_payload)
        elif self.tool_kind == "diagnostic":
            journal.artifact("tool_output_full", {
                "tool": self.call.name,
                "args": Message.redact_payload(self.call.args),
                "result": Message.redact_payload(raw_result),
            })
        state = self.tool_state
        journal.store.finish_tool_call(
            journal.run_id,
            self.call.id,
            state.persisted_status if state else self.outcome.status,
            Message.redact_payload(raw_result),
            Message.redact_payload(sent_result),
            len(sent_result) < len(raw_result),
            self.outcome.approved_by,
            self.outcome.call_error,
            phase=state.phase.value if state else None,
            result_code=state.result_code if state else self.outcome.result_code,
            reason=state.reason if state else None,
            violation_kind=state.violation_kind if state else None,
        )
        tool_message.persist(journal, backend=backend, tool_call_id=tool_message.tool_call_id)
        journal.event({
            "type": "tool",
            "name": self.call.name,
            "args": Message.redact_payload(self.call.args),
            "result": Message.redact_payload(sent_result),
            "status": self.outcome.status,
            "mutating": self.call.mutating,
        })
        return tool_message


def _unknown_tool(name: str) -> ToolOutcome:
    message = f"unknown tool: {name}"
    return ToolOutcome(message=message, status="failed", result_code="unknown_tool", call_error=message)
