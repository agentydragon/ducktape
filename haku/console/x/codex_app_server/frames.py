"""The small Codex app-server vocabulary the Console runtime acts on directly.

Projection owns item and delta interpretation.  The connected client needs only request methods and
thread/turn identities; the runtime needs only prompt and terminal recognition.  Keeping those
native names here prevents either from growing a second ad-hoc parser.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

INITIALIZE: Final = "initialize"
INITIALIZED: Final = "initialized"
THREAD_LOADED_LIST: Final = "thread/loaded/list"
THREAD_READ: Final = "thread/read"
THREAD_START: Final = "thread/start"
TURN_START: Final = "turn/start"
TURN_INTERRUPT: Final = "turn/interrupt"
TURN_COMPLETED: Final = "turn/completed"
THREAD_STATUS_CHANGED: Final = "thread/status/changed"


def method(frame: Mapping[str, Any]) -> str | None:
    value = frame.get("method")
    return value if isinstance(value, str) else None


def is_prompt(frame: Mapping[str, Any]) -> bool:
    return method(frame) == TURN_START and "id" in frame


def terminal_turn(frame: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if method(frame) != TURN_COMPLETED:
        return None
    params = frame.get("params")
    turn = params.get("turn") if isinstance(params, dict) else None
    return turn if isinstance(turn, dict) else None


def system_error(frame: Mapping[str, Any]) -> bool:
    """Whether *frame* is Codex declaring the thread unusable.

    `ThreadStatus` is `notLoaded | idle | systemError | active`, and Codex sends `systemError` when
    it gives up on the thread rather than on one turn — in the captured production failure, one
    frame before the terminal error (`docs/protocol_evidence.md` § Turn failures).
    """
    if method(frame) != THREAD_STATUS_CHANGED:
        return False
    params = frame.get("params")
    status = params.get("status") if isinstance(params, dict) else None
    return isinstance(status, dict) and status.get("type") == "systemError"


def nested_string(value: Mapping[str, Any], *path: str) -> str:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            raise ValueError(f"missing response field: {'.'.join(path)}")
        current = current.get(key)
    if not isinstance(current, str):
        raise ValueError(f"missing response field: {'.'.join(path)}")
    return current
