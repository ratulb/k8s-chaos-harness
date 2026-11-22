from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from tools.truncate import truncate as truncate_text


@dataclass(frozen=True)
class Message:
    role: str
    content: str | None = None
    tool_calls: tuple[dict[str, Any], ...] = ()
    tool_call_id: str | None = None

    @staticmethod
    def redact(message: dict[str, Any]) -> dict[str, Any]:
        redacted = json.loads(json.dumps(message))
        for call in redacted.get("tool_calls") or []:
            function = call.get("function") or {}
            raw = function.get("arguments")
            if not raw:
                continue
            try:
                args = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(args, dict) and "approval_token" in args:
                args.pop("approval_token", None)
                function["arguments"] = json.dumps(args)
        return redacted

    @staticmethod
    def redact_payload(payload: Any) -> Any:
        redacted = json.loads(json.dumps(payload, default=str))
        pattern = re.compile(r"approved:[A-Za-z0-9_.:-]+")

        def visit(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: "[REDACTED]" if key == "approval_token" else visit(item)
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [visit(item) for item in value]
            if isinstance(value, str):
                return pattern.sub("[REDACTED]", value)
            return value

        return visit(redacted)

    @staticmethod
    def truncate(message: dict[str, Any], limit: int) -> dict[str, Any]:
        truncated = json.loads(json.dumps(message))
        content = truncated.get("content")
        if isinstance(content, str):
            truncated["content"] = truncate_text(content, limit)
        return truncated

    @classmethod
    def system(cls, content: str) -> "Message":
        return cls("system", content)

    @classmethod
    def user(cls, content: str) -> "Message":
        return cls("user", content)

    @classmethod
    def assistant(cls, content: str | None = None, tool_calls: list[dict[str, Any]] | None = None) -> "Message":
        return cls("assistant", content, tuple(tool_calls or ()))

    @classmethod
    def tool(cls, content: str, tool_call_id: str) -> "Message":
        return cls("tool", content, tool_call_id=tool_call_id)

    @classmethod
    def from_tool_result(cls, content: str, call_id: str, limit: int | None = None) -> "Message":
        message = cls.tool(content, call_id)
        if limit is None:
            return message
        return cls.from_dict(cls.truncate(message.to_dict(), limit))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        return cls(
            role=str(data.get("role", "unknown")),
            content=data.get("content"),
            tool_calls=tuple(data.get("tool_calls") or ()),
            tool_call_id=data.get("tool_call_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            data["tool_calls"] = json.loads(json.dumps(list(self.tool_calls)))
        if self.tool_call_id is not None:
            data["tool_call_id"] = self.tool_call_id
        return data

    def to_stored_dict(self) -> dict[str, Any]:
        return Message.redact_payload(self.to_dict())

    def persist(
        self,
        journal: "Journal",
        backend: str | None = None,
        tokens: int | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        stored = self.to_stored_dict()
        journal.store.append_message(
            journal.run_id,
            stored["role"],
            stored.get("content"),
            stored.get("tool_calls") or None,
            tool_call_id=stored.get("tool_call_id") if tool_call_id is None else tool_call_id,
            tokens=tokens,
            backend=backend,
        )
