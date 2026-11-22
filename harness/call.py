from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Call:
    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    msg_seq: int = 0
    mutating: bool = False
    destructive: bool = False

    @classmethod
    def from_tool_call(cls, payload: dict[str, Any], msg_seq: int) -> "Call":
        function = payload.get("function") or {}
        raw_args = function.get("arguments") or "{}"
        import json

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
