"""Wire models for claude.ai's private `/v1/code/sessions` API (contract: docs/api.md)."""

from pydantic import BaseModel, ConfigDict, Field


class Event(BaseModel):
    """One session event. Unmodelled fields ride along so the archive stays lossless."""

    model_config = ConfigDict(extra="allow")

    event_id: str
    sequence_num: str = Field(description="Decimal string on the wire; kept as sent so the archive is lossless.")

    @property
    def seq(self) -> int:
        return int(self.sequence_num)


class EventsPage(BaseModel):
    data: list[Event]
    next_cursor: str | None = Field(default=None, description="Absent when this page is the last one.")


class SessionSummary(BaseModel):
    """A `/v1/code/sessions` list item; `index.jsonl` stores it as sent."""

    model_config = ConfigDict(extra="allow")

    id: str
    last_event_at: str


class SessionsPage(BaseModel):
    data: list[SessionSummary]
    next_cursor: str | None = Field(default=None, description="Absent when this page is the last one.")
