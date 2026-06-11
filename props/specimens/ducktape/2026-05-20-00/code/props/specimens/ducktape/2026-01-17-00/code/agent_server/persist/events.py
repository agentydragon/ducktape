from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from agent_core.events import EventType


class EventRecord(BaseModel):
    """Event record: sequence number, timestamp, and typed event.

    The EventType payload already contains all relevant fields (call_id, tool_key, etc.).
    """

    seq: int
    ts: datetime
    payload: EventType

    model_config = ConfigDict(extra="forbid")


def parse_event(d: dict[str, Any]) -> EventRecord:
    return EventRecord.model_validate(d)
