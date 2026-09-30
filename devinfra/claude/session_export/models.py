"""Wire models for claude.ai's private `/v1/code/sessions` API (contract: docs/api.md).

Timestamps stay strings as sent, so the archive is lossless; `parse_timestamp` converts where a caller needs a time.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# Every event of the observed archive carries this and a null `sent_by_account_id`; the sync stores neither and
# refuses an event that differs (see store.py).
DEFAULT_ATTESTATION_STATUS = "DEVICE_ATTESTATION_STATUS_UNSPECIFIED"


SESSION_STATUS_ARCHIVED = "archived"


def canonical_id(session_id: str) -> str:
    """The API accepts `session_<x>` and `cse_<x>` interchangeably; files and the database use `session_<x>`."""
    return "session_" + session_id.split("_", 1)[1]


def cse_id(session_id: str) -> str:
    """The form the web client puts in the event-stream URL."""
    return "cse_" + session_id.split("_", 1)[1]


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp without a UTC offset: {value=}")
    return parsed


class Event(BaseModel):
    """One session event. Unmodelled fields ride along so the archive stays lossless."""

    model_config = ConfigDict(extra="allow")

    event_id: str
    sequence_num: str = Field(description="Decimal string on the wire; kept as sent so the archive is lossless.")
    event_type: str
    source: str
    created_at: str
    received_at: str | None = Field(default=None, description="Set once the worker's queue has the event.")
    processing_at: str | None = Field(default=None, description="Set once the worker has started on the event.")
    processed_at: str | None = Field(default=None, description="Set once the worker has finished the event.")
    device_attestation_status: str = DEFAULT_ATTESTATION_STATUS
    sent_by_account_id: str | None = None
    payload: dict[str, Any]

    @property
    def seq(self) -> int:
        return int(self.sequence_num)


class DeliveryUpdate(BaseModel):
    """The `delivery_update` frame: the worker's queue moved a client-sent event to another status.

    The status is not read. A stream pushes the event without its worker stamps, and it is not sent again when they
    change; this frame is the notice, and the stamps are on the event as the events route reports it.
    """

    event_id: UUID


class EventsPage(BaseModel):
    data: list[Event]
    next_cursor: str | None = Field(default=None, description="Absent when this page is the last one.")


class SessionSummary(BaseModel):
    """A `/v1/code/sessions` list item; `index.jsonl` stores it as sent."""

    model_config = ConfigDict(extra="allow")

    id: str
    title: str
    status: str
    created_at: str
    updated_at: str
    last_event_at: str


class SessionsPage(BaseModel):
    data: list[SessionSummary]
    next_cursor: str | None = Field(default=None, description="Absent when this page is the last one.")


class SessionRemoved(BaseModel):
    """The `removed` frame of the session watch."""

    id: str


class ResumeTokenPage(BaseModel):
    """What a list page carries beyond its sessions that the session watch needs."""

    resume_token: str = Field(description="Where in the change feed the listed state stands; opens the watch.")
