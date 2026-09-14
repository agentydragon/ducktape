"""Durable, replayable output from the one bootstrap initialization a sandbox may select."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Sequence
from pathlib import Path

from google.protobuf.json_format import MessageToDict, ParseDict

from x.agentplane.runner import protocol_pb2 as pb


class InitializationLog:
    """An append-only initialization event log with dense reconnect cursors."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._events: list[pb.InitializationEvent] = []
        if path.exists():
            self._load()
        self._file = path.open("ab")
        self._changed = asyncio.Event()

    def _load(self) -> None:
        data = self.path.read_bytes()
        offset = 0
        for raw in data.split(b"\n"):
            line = raw.strip()
            if line:
                try:
                    self._events.append(ParseDict(json.loads(line), pb.InitializationEvent()))
                except ValueError as error:
                    if offset + len(raw) < len(data):
                        raise ValueError(f"corrupt initialization log {self.path} at byte {offset}") from error
                    with self.path.open("r+b") as existing:
                        existing.truncate(offset)
                    return
            offset += len(raw) + 1

    @property
    def last_sequence(self) -> int:
        return self._events[-1].sequence if self._events else 0

    @property
    def last_attempt(self) -> int:
        return self._events[-1].attempt if self._events else 0

    @property
    def completed(self) -> bool:
        return bool(self._events and self._events[-1].HasField("result") and self._events[-1].result.exit_code == 0)

    def since(self, after_sequence: int) -> Sequence[pb.InitializationEvent]:
        return self._events[after_sequence:]

    def append_output(self, attempt: int, stream: pb.InitializationStream, data: bytes) -> None:
        self._append(
            pb.InitializationEvent(
                sequence=self.last_sequence + 1,
                attempt=attempt,
                output=pb.InitializationOutput(stream=stream, data=data),
            )
        )

    def append_result(self, attempt: int, exit_code: int) -> pb.InitializeResult:
        result = pb.InitializeResult(executed=True, exit_code=exit_code)
        self._append(pb.InitializationEvent(sequence=self.last_sequence + 1, attempt=attempt, result=result))
        return result

    def _append(self, event: pb.InitializationEvent) -> None:
        self._file.write(json.dumps(MessageToDict(event, preserving_proto_field_name=True)).encode() + b"\n")
        self._file.flush()
        os.fsync(self._file.fileno())
        self._events.append(event)
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    async def wait_beyond(self, sequence: int) -> None:
        while self.last_sequence <= sequence:
            await self._changed.wait()

    def close(self) -> None:
        self._file.close()
