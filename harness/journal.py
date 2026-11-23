from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from harness.message import Message
from harness.ports import RunStore


@dataclass(frozen=True)
class Journal:
    run_id: str
    store: RunStore
    reporter: Any = None

    def event(self, event: dict[str, Any]) -> int:
        event["ts"] = time.time()
        event_type = str(event.get("type", "unknown"))
        redacted = Message.redact_payload(event)
        self.emit(event_type, self.describe(event_type, redacted))
        stored_event = dict(redacted)
        if event_type == "approval" and "approval_token" in event:
            stored_event["approval_token"] = event["approval_token"]
        return int(self.store.add_event(self.run_id, event_type, stored_event))

    def artifact(self, kind: str, payload: Any, content_type: str = "application/json") -> None:
        content = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True, default=str)
        self.store.add_artifact(self.run_id, kind, content, content_type)

    def emit(self, kind: str, text: Any) -> None:
        if self.reporter is None:
            return
        try:
            self.reporter.emit(kind, text)
        except Exception:
            pass

    @staticmethod
    def describe(event_type: str, event: dict[str, Any]) -> str:
        if event_type == "assistant":
            message = event.get("message") or {}
            return message.get("content") or message.get("reasoning") or ""
        if event_type == "tool":
            return f"{event.get('name', '?')}: {event.get('status', '?')} {event.get('result', '')}"
        if event_type == "approval":
            return f"diagnosis={event.get('diagnosis', '?')} granted={event.get('granted')}"
        return str(event)
