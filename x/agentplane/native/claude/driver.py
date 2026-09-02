"""Explicit native Claude stream/control frame constructors; no neutral facade."""

from __future__ import annotations

from uuid import uuid4

from x.agentplane.native.claude import wire


def initialize() -> wire.InitializeRequest:
    return wire.InitializeRequest()


def user_frame(text: str, *, message_uuid: str | None = None) -> wire.UserInput:
    return wire.UserInput(message=wire.UserMessage(role="user", content=text), uuid=message_uuid or str(uuid4()))


def interrupt(*, cancel_queued: bool, reason: str = "capture") -> wire.InterruptRequest:
    return wire.InterruptRequest(request=wire.InterruptBody(reason=reason, cancel_queued=cancel_queued))
