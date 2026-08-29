"""Codex app-server envelopes and the runner's method vocabulary.

The envelope and payload types come from ``generated_protocol``, which is built from the Codex
binary pinned in ``MODULE.bazel``.  Parsing stays fail-soft at this boundary: an unknown method or
newly-shaped message is retained as ``UnknownMessage`` so a Codex upgrade cannot crash projection.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from pydantic import ValidationError

from haku.runner.codex.generated_protocol import JSONRPCError, JSONRPCNotification, JSONRPCRequest, JSONRPCResponse

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
class UnknownMessage:
    reason: str
    raw: JsonObject


type Message = JSONRPCRequest | JSONRPCNotification | JSONRPCResponse | JSONRPCError | UnknownMessage


def parse_message(payload: Mapping[str, Any]) -> Message:
    """Parse one Codex envelope using the generated authoritative models."""
    raw = dict(payload)
    method = raw.get("method")
    request_id = raw.get("id")
    if isinstance(method, str):
        if isinstance(request_id, (int, str)) and not isinstance(request_id, bool):
            try:
                return JSONRPCRequest.model_validate(raw)
            except ValidationError:
                return UnknownMessage(reason=f"{method}/request", raw=raw)
        if "id" not in raw:
            try:
                return JSONRPCNotification.model_validate(raw)
            except ValidationError:
                return UnknownMessage(reason=f"{method}/notification", raw=raw)
        return UnknownMessage(reason=f"{method}/id", raw=raw)

    if isinstance(request_id, (int, str)) and not isinstance(request_id, bool):
        if "error" in raw:
            try:
                return JSONRPCError.model_validate(raw)
            except ValidationError:
                return UnknownMessage(reason="response/error", raw=raw)
        if "result" in raw:
            try:
                return JSONRPCResponse.model_validate(raw)
            except ValidationError:
                return UnknownMessage(reason="response/result", raw=raw)

    return UnknownMessage(reason="envelope", raw=raw)
