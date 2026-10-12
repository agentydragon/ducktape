"""Durable output from the one bootstrap attempt a sandbox may make."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path

from google.protobuf.json_format import MessageToDict, ParseDict

from agentplane.proto import runner_pb2


class InitializationLog:
    """An append-only initialization event log with dense sequences."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._events: list[runner_pb2.InitializationEvent] = []
        if path.exists():
            self._load()
        self._file = path.open("ab")

    def _load(self) -> None:
        data = self.path.read_bytes()
        offset = 0
        for raw in data.split(b"\n"):
            line = raw.strip()
            if line:
                try:
                    self._events.append(ParseDict(json.loads(line), runner_pb2.InitializationEvent()))
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
    def ended(self) -> bool:
        """The attempt has a recorded result or interruption. Nothing is appended after either."""
        return bool(self._events and self._events[-1].WhichOneof("observation") in ("result", "interrupted"))

    def since(self, after_sequence: int) -> Sequence[runner_pb2.InitializationEvent]:
        return self._events[after_sequence:]

    def append_output(self, attempt: int, stream: runner_pb2.InitializationStream, data: bytes) -> None:
        self._append(
            runner_pb2.InitializationEvent(
                sequence=self.last_sequence + 1,
                attempt=attempt,
                output=runner_pb2.InitializationOutput(stream=stream, data=data),
            )
        )

    def append_result(self, attempt: int, exit_code: int) -> runner_pb2.InitializeResult:
        result = runner_pb2.InitializeResult(executed=True, exit_code=exit_code)
        self._append(runner_pb2.InitializationEvent(sequence=self.last_sequence + 1, attempt=attempt, result=result))
        return result

    def append_interrupted(self, attempt: int) -> None:
        self._append(
            runner_pb2.InitializationEvent(
                sequence=self.last_sequence + 1, attempt=attempt, interrupted=runner_pb2.InitializationInterrupted()
            )
        )

    def _append(self, event: runner_pb2.InitializationEvent) -> None:
        self._file.write(json.dumps(MessageToDict(event, preserving_proto_field_name=True)).encode() + b"\n")
        self._file.flush()
        os.fsync(self._file.fileno())
        self._events.append(event)

    def close(self) -> None:
        self._file.close()
