"""Explicit native Claude stream/control frame constructors; no neutral facade."""

from __future__ import annotations

from typing import Any
from uuid import uuid4


def initialize() -> dict[str, Any]:
    return {"type": "control_request", "request_id": f"capture-{uuid4().hex}", "request": {"subtype": "initialize"}}


def user_frame(text: str, *, message_uuid: str | None = None) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
        "parent_tool_use_id": None,
        "uuid": message_uuid or str(uuid4()),
    }


def interrupt(*, cancel_queued: bool) -> dict[str, Any]:
    return {
        "type": "control_request",
        "request_id": f"capture-{uuid4().hex}",
        "request": {"subtype": "interrupt", "reason": "capture", "cancel_queued": cancel_queued},
    }
