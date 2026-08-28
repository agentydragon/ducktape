"""The Codex app-server JSONL protocol the runner speaks, runner-side.

Fail-soft envelope parsing for the Codex 0.144.1 app-server, and the small method vocabulary the
run-loop acts on directly. The wire is JSON-RPC-shaped without a required ``jsonrpc`` member; this
module parses only the envelope, so a protocol release can add fields without making an older
projection crash. The projector (<projection.py>) validates exactly the fields it consumes.

Ported runner-side from the Console client that drove Codex over the bridge before the #4667 cut,
so the runner interprets the stream the Console no longer does. Protocol evidence is pinned at
``@openai/codex@0.144.1`` (upstream tag ``rust-v0.144.1``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

type JsonObject = dict[str, Any]
type RequestId = int | str

INITIALIZE: Final = "initialize"
INITIALIZED: Final = "initialized"
THREAD_START: Final = "thread/start"
TURN_START: Final = "turn/start"
TURN_INTERRUPT: Final = "turn/interrupt"
TURN_COMPLETED: Final = "turn/completed"
THREAD_STATUS_CHANGED: Final = "thread/status/changed"


@dataclass(frozen=True, slots=True)
class Request:
    request_id: RequestId
    method: str
    params: JsonObject | None
    raw: JsonObject


@dataclass(frozen=True, slots=True)
class Notification:
    method: str
    params: JsonObject | None
    raw: JsonObject


@dataclass(frozen=True, slots=True)
class Response:
    request_id: RequestId
    result: Any
    error: JsonObject | None
    raw: JsonObject


@dataclass(frozen=True, slots=True)
class UnknownMessage:
    reason: str
    raw: JsonObject


type Message = Request | Notification | Response | UnknownMessage


def parse_message(payload: Mapping[str, Any]) -> Message:
    """Parse one app-server envelope without rejecting future methods or extra fields."""
    raw = dict(payload)
    method = raw.get("method")
    request_id = raw.get("id")
    if isinstance(method, str):
        params = raw.get("params")
        if params is not None and not isinstance(params, dict):
            return UnknownMessage(reason=f"{method}/params", raw=raw)
        if isinstance(request_id, (int, str)) and not isinstance(request_id, bool):
            return Request(request_id=request_id, method=method, params=params, raw=raw)
        if "id" not in raw:
            return Notification(method=method, params=params, raw=raw)
        return UnknownMessage(reason=f"{method}/id", raw=raw)

    if isinstance(request_id, (int, str)) and not isinstance(request_id, bool):
        error = raw.get("error")
        if error is not None and not isinstance(error, dict):
            return UnknownMessage(reason="response/error", raw=raw)
        if "result" in raw or error is not None:
            return Response(request_id=request_id, result=raw.get("result"), error=error, raw=raw)

    return UnknownMessage(reason="envelope", raw=raw)


def nested_string(value: Mapping[str, Any], *path: str) -> str:
    """The string at *path* in a nested response object, or a `ValueError` naming the missing key."""
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            raise ValueError(f"missing response field: {'.'.join(path)}")
        current = current.get(key)
    if not isinstance(current, str):
        raise ValueError(f"missing response field: {'.'.join(path)}")
    return current
