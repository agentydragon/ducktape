"""Fail-soft parsing for the Codex 0.144.1 app-server JSONL protocol.

The wire is JSON-RPC-shaped without a required ``jsonrpc`` member.  This module deliberately
parses only the envelope: notification parameter shapes remain dictionaries so a protocol release
can add fields without making an older Haku projection crash.  The notification projector in
``projection.py`` validates exactly the fields it consumes.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

type JsonObject = dict[str, Any]
type RequestId = int | str


class Direction(StrEnum):
    CLIENT_TO_SERVER = "client_to_server"
    SERVER_TO_CLIENT = "server_to_client"


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


@dataclass(frozen=True, slots=True)
class TraceRecord:
    """One direction-labelled JSONL record written by the capture utility."""

    seq: int
    direction: Direction
    message: JsonObject


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


def read_trace(path: Path) -> tuple[TraceRecord, ...]:
    """Read a bounded, already-sanitized capture fixture.

    Blank lines are ignored.  Invalid JSON and malformed trace wrappers are fixture errors rather
    than wire additions, so they raise ``ValueError`` with the source line number.
    """
    records: list[TraceRecord] = []
    previous_seq = 0
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: record is not an object")
        # The capture utility writes ``seq``/``message``.  The reviewed staging capture predates
        # that wrapper and uses ``payload`` with the JSONL line as its stable position.  Accepting
        # both keeps the real artifact verbatim while retaining an explicit sequence for newly
        # captured traces.
        seq = value.get("seq", line_number)
        message = value.get("message", value.get("payload"))
        direction = value.get("direction")
        if (
            not isinstance(seq, int)
            or isinstance(seq, bool)
            or seq <= previous_seq
            or not isinstance(message, dict)
            or not isinstance(direction, str)
        ):
            raise ValueError(f"{path}:{line_number}: malformed trace record")
        try:
            parsed_direction = Direction(direction)
        except ValueError as exc:
            raise ValueError(f"{path}:{line_number}: unknown direction {direction!r}") from exc
        records.append(TraceRecord(seq=seq, direction=parsed_direction, message=message))
        previous_seq = seq
    return tuple(records)


def server_messages(records: Iterable[TraceRecord]) -> Iterator[TraceRecord]:
    """Only app-server output; client requests are evidence, not conversation events."""
    return (record for record in records if record.direction is Direction.SERVER_TO_CLIENT)
