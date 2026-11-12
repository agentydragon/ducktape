"""Append-only diagnostics; never a source of conversation messages."""

from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO
from uuid import UUID, uuid4

from pydantic import BaseModel, JsonValue


class Event(BaseModel):
    session_id: UUID
    recorded_at: datetime
    kind: str
    run_id: UUID | None
    step: int | None
    data: dict[str, JsonValue]


class Trace:
    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.session_id = uuid4()

    def record(
        self,
        kind: str,
        *,
        run_id: UUID | None = None,
        step: int | None = None,
        data: dict[str, JsonValue] | None = None,
    ) -> None:
        event = Event(
            session_id=self.session_id,
            recorded_at=datetime.now(UTC),
            kind=kind,
            run_id=run_id,
            step=step,
            data=data if data is not None else {},
        )
        self.stream.write(event.model_dump_json() + "\n")
        self.stream.flush()


def read_trace(path: Path) -> list[Event]:
    with path.open() as stream:
        return [Event.model_validate_json(line) for line in stream]
