from __future__ import annotations

import json
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
