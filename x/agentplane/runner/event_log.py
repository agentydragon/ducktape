"""The runner-session Event source: durable EventEntries followed from a cursor."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.message import Message

from x.agentplane.protocol import event_log_pb2, event_pb2
from x.agentplane.runner.journal_file import JournalFile

logger = logging.getLogger(__name__)

Observation = (
    event_pb2.HarnessStarted
    | event_pb2.HarnessExited
    | event_pb2.HarnessLost
    | event_pb2.HarnessStderr
    | event_pb2.CommandAdmitted
    | event_pb2.CommandFailed
    | event_pb2.CommandNoop
    | event_pb2.HarnessUserMessageConfirmed
    | event_pb2.ModelChanged
    | event_pb2.TurnStarted
    | event_pb2.TurnCompleted
    | event_pb2.ItemStarted
    | event_pb2.TextDelta
    | event_pb2.ToolArgumentsDelta
    | event_pb2.ToolArguments
    | event_pb2.ToolOutputDelta
    | event_pb2.ItemCompleted
    | event_pb2.Native
    | event_pb2.DebugCheckpoint
)

_FIELDS: dict[type[Message], str] = {
    event_pb2.HarnessStarted: "harness_started",
    event_pb2.HarnessExited: "harness_exited",
    event_pb2.HarnessLost: "harness_lost",
    event_pb2.HarnessStderr: "harness_stderr",
    event_pb2.CommandAdmitted: "command_admitted",
    event_pb2.CommandFailed: "command_failed",
    event_pb2.CommandNoop: "command_noop",
    event_pb2.HarnessUserMessageConfirmed: "harness_user_message_confirmed",
    event_pb2.ModelChanged: "model_changed",
    event_pb2.TurnStarted: "turn_started",
    event_pb2.TurnCompleted: "turn_completed",
    event_pb2.ItemStarted: "item_started",
    event_pb2.TextDelta: "text_delta",
    event_pb2.ToolArgumentsDelta: "tool_arguments_delta",
    event_pb2.ToolArguments: "tool_arguments",
    event_pb2.ToolOutputDelta: "tool_output_delta",
    event_pb2.ItemCompleted: "item_completed",
    event_pb2.Native: "native",
    event_pb2.DebugCheckpoint: "debug_checkpoint",
}
_OBSERVATIONS: dict[str, type[Message]] = {field: message for message, field in _FIELDS.items()}


class EventLog:
    def __init__(self, path: Path, source_id: str) -> None:
        self.path = path
        self.source_id = source_id
        self._entries: list[event_log_pb2.EventEntry] = []
        if path.exists():
            self._load()
        self._file = JournalFile(path)
        self._changed = asyncio.Event()

    def _load(self) -> None:
        """Load entries, dropping only a trailing interrupted append."""
        data = self.path.read_bytes()
        offset = 0
        for raw in data.split(b"\n"):
            line = raw.strip()
            if line:
                try:
                    entry = ParseDict(json.loads(line), event_log_pb2.EventEntry())
                    if (
                        entry.cursor != len(self._entries) + 1
                        or entry.origin.source_id != self.source_id
                        or entry.origin.sequence != entry.cursor
                    ):
                        raise ValueError("invalid runner EventEntry origin or cursor")
                    self._entries.append(entry)
                except ValueError as error:
                    if offset + len(raw) < len(data):
                        raise ValueError(f"corrupt session log {self.path} at byte {offset}") from error
                    logger.warning("%s: dropping an incomplete final line of %d bytes", self.path, len(raw))
                    with self.path.open("r+b") as existing:
                        existing.truncate(offset)
                    return
            offset += len(raw) + 1

    @property
    def last_cursor(self) -> int:
        return self._entries[-1].cursor if self._entries else 0

    @property
    def entries(self) -> Sequence[event_log_pb2.EventEntry]:
        return self._entries

    def append(self, observation: Observation, *, sources: Sequence[int] = ()) -> event_log_pb2.EventEntry:
        cursor = self.last_cursor + 1
        event = event_pb2.Event(source_sequences=list(sources))
        event.at.FromDatetime(datetime.now(UTC))
        getattr(event, _FIELDS[type(observation)]).CopyFrom(observation)
        entry = event_log_pb2.EventEntry(
            cursor=cursor, origin=event_log_pb2.EventOrigin(source_id=self.source_id, sequence=cursor), event=event
        )
        try:
            self._file.append(json.dumps(MessageToDict(entry, preserving_proto_field_name=True)).encode() + b"\n")
        except OSError:
            self._changed.set()
            raise
        self._entries.append(entry)
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()
        return entry

    def since(self, after_cursor: int) -> list[event_log_pb2.EventEntry]:
        """Entries with a cursor greater than `after_cursor`, in order."""
        return self._entries[after_cursor:]

    async def wait_beyond(self, cursor: int) -> None:
        while self.last_cursor <= cursor:
            self._file.check_writable()
            await self._changed.wait()

    def close(self) -> None:
        self._file.close()


def encode_observation(observation: Observation) -> dict[str, object]:
    """Serialize an observation without fabricating an EventEntry cursor or timestamp."""
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
