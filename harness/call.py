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
        journal.store.begin_tool_call(
            journal.run_id,
            self.id,
            self.msg_seq,
            self.name,
            Message.redact_payload(self.args),
            self.mutating,
            self.destructive,
        )
        if tool is None:
            return CallExecution.from_outcome(self, self.kind, _unknown_tool(self.name))
        if tool.kind == "diagnostic":
            journal.store.update_tool_call_status(journal.run_id, self.id, "started")
        context.call_id = self.id
        context.call = self
        try:
            outcome = tool.run(self.args, context)
        except Exception as exc:
            outcome = ToolOutcome(message=f"ERROR: {exc}", status="failed", call_error=str(exc))
        return CallExecution.from_outcome(self, self.kind, outcome)


@dataclass(frozen=True)
class CallExecution:
    call: Call
    tool_kind: str
    outcome: ToolOutcome
    result: Result[ToolOutcome]

    @classmethod
    def from_outcome(cls, call: Call, tool_kind: str, outcome: ToolOutcome) -> "CallExecution":
        failed = outcome.status == "failed" or bool(outcome.call_error)
        if failed and outcome.call_error is None:
            outcome = replace(outcome, call_error=outcome.message)
        result = (
            Result.failure(outcome.call_error or outcome.message)
            if failed
            else Result.success(outcome)
        )
        return cls(call=call, tool_kind=tool_kind, outcome=outcome, result=result)

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
        journal.store.finish_tool_call(
            journal.run_id,
            self.call.id,
            self.outcome.status,
            Message.redact_payload(raw_result),
            Message.redact_payload(sent_result),
            len(sent_result) < len(raw_result),
            self.outcome.approved_by,
            self.outcome.call_error,
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
    return ToolOutcome(message=message, status="failed", call_error=message)
