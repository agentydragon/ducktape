"""The session log: every Event, appended durably and replayable from any sequence.

One JSON line per Event, in proto JSON with proto field names, so the file reads with any JSON
tool and parses back into the same message. The whole log is also held in memory: sessions are
bounded by one conversation, and an attachment replaying from a cursor reads the list directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.message import Message

from x.agentplane.runner import protocol_pb2 as pb

logger = logging.getLogger(__name__)

Observation = (
    pb.HarnessStarted
    | pb.HarnessExited
    | pb.HarnessLost
    | pb.HarnessStderr
    | pb.CommandReceived
    | pb.CommandRejected
    | pb.CommandNoop
    | pb.HarnessUserMessageConfirmed
    | pb.ModelChanged
    | pb.TurnStarted
    | pb.TurnCompleted
    | pb.ItemStarted
    | pb.TextDelta
    | pb.ToolArgumentsDelta
    | pb.ToolArguments
    | pb.ToolOutputDelta
    | pb.ItemCompleted
    | pb.Native
    | pb.DebugCheckpoint
)

_FIELDS: dict[type[Message], str] = {
    pb.HarnessStarted: "harness_started",
    pb.HarnessExited: "harness_exited",
    pb.HarnessLost: "harness_lost",
    pb.HarnessStderr: "harness_stderr",
    pb.CommandReceived: "command_received",
    pb.CommandRejected: "command_rejected",
    pb.CommandNoop: "command_noop",
    pb.HarnessUserMessageConfirmed: "harness_user_message_confirmed",
    pb.ModelChanged: "model_changed",
    pb.TurnStarted: "turn_started",
    pb.TurnCompleted: "turn_completed",
    pb.ItemStarted: "item_started",
    pb.TextDelta: "text_delta",
    pb.ToolArgumentsDelta: "tool_arguments_delta",
    pb.ToolArguments: "tool_arguments",
    pb.ToolOutputDelta: "tool_output_delta",
    pb.ItemCompleted: "item_completed",
    pb.Native: "native",
    pb.DebugCheckpoint: "debug_checkpoint",
}
_OBSERVATIONS: dict[str, type[Message]] = {field: message for message, field in _FIELDS.items()}

# Events a restarted runner reasons from are synced to disk before they are reported; deltas and
# native evidence are flushed but not synced, since losing a tail of them only shortens the record.
_SYNCED = (
    pb.HarnessStarted,
    pb.HarnessExited,
    pb.HarnessLost,
    pb.CommandReceived,
    pb.CommandRejected,
    pb.CommandNoop,
    pb.HarnessUserMessageConfirmed,
    pb.ModelChanged,
    pb.TurnStarted,
    pb.TurnCompleted,
    pb.DebugCheckpoint,
)


class EventLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._events: list[pb.Event] = []
        if path.exists():
            self._load()
        self._file = path.open("ab")
        self._changed = asyncio.Event()

    def _load(self) -> None:
        """Read the log back. A final line an interrupted append left incomplete is dropped and
        truncated away, so the next append starts a fresh line; a bad line anywhere else is
        corruption and refuses the log."""
        data = self.path.read_bytes()
        offset = 0
        for raw in data.split(b"\n"):
            line = raw.strip()
            if line:
                try:
                    self._events.append(ParseDict(json.loads(line), pb.Event()))
                except ValueError as error:
                    if offset + len(raw) < len(data):
                        raise ValueError(f"corrupt session log {self.path} at byte {offset}") from error
                    logger.warning("%s: dropping an incomplete final line of %d bytes", self.path, len(raw))
                    with self.path.open("r+b") as existing:
                        existing.truncate(offset)
                    return
            offset += len(raw) + 1

    @property
    def last_sequence(self) -> int:
        return self._events[-1].sequence if self._events else 0

    @property
    def events(self) -> Sequence[pb.Event]:
        return self._events

    def append(self, observation: Observation, *, sources: Sequence[int] = ()) -> pb.Event:
        event = pb.Event(sequence=self.last_sequence + 1, source_sequences=list(sources))
        event.at.FromDatetime(datetime.now(UTC))
        getattr(event, _FIELDS[type(observation)]).CopyFrom(observation)
        self._file.write(json.dumps(MessageToDict(event, preserving_proto_field_name=True)).encode() + b"\n")
        self._file.flush()
        if isinstance(observation, _SYNCED):
            os.fsync(self._file.fileno())
        self._events.append(event)
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()
        return event

    def since(self, after_sequence: int) -> list[pb.Event]:
        """Events with a sequence greater than `after_sequence`, in order."""
        # Sequences are dense from 1, so the cursor is an index.
        return self._events[after_sequence:]

    async def wait_beyond(self, sequence: int) -> None:
        while self.last_sequence <= sequence:
            await self._changed.wait()

    def close(self) -> None:
        self._file.close()


def encode_observation(observation: Observation) -> dict[str, object]:
    """Serialize an observation without fabricating an Event sequence or timestamp.

    The command journal uses this for a terminal result it has observed but whose public Event append
    may be interrupted. The Event log remains the public sequence allocator on replay.
    """
    return {"kind": _FIELDS[type(observation)], "value": MessageToDict(observation, preserving_proto_field_name=True)}


def decode_observation(raw: object) -> Observation:
    """Parse the journal form of one observation; reject malformed support state strictly."""
    if not isinstance(raw, dict):
        raise ValueError("observation must be an object")
    kind, value = raw.get("kind"), raw.get("value")
    message = _OBSERVATIONS.get(kind) if isinstance(kind, str) else None
    if message is None or not isinstance(value, dict):
        raise ValueError("observation has an invalid kind or value")
    return cast(Observation, ParseDict(value, message()))
