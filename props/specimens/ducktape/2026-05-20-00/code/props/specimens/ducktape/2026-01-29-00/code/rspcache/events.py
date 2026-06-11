from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from pydantic.type_adapter import TypeAdapter

from rspcache.models import ResponseStatus


class EventBase(BaseModel):
    """Base class for rspcache events emitted over PG NOTIFY."""

    type: str


class ResponseStatusEvent(EventBase):
    type: Literal["response_status"] = "response_status"
    cache_key: str
    response_id: str | None = None
    status: ResponseStatus
    error: str | None = None


class FrameAppendedEvent(EventBase):
    type: Literal["frame"] = "frame"
    cache_key: str
    response_id: str | None = None
    sequence_number: int
    frame_type: str | None = None


class APIKeyCreatedEvent(EventBase):
    type: Literal["api_key_created"] = "api_key_created"
    id: str
    name: str
    upstream_alias: str


class APIKeyRevokedEvent(EventBase):
    type: Literal["api_key_revoked"] = "api_key_revoked"
    id: str


EventPayload = ResponseStatusEvent | FrameAppendedEvent | APIKeyCreatedEvent | APIKeyRevokedEvent

_EVENT_ADAPTER: TypeAdapter[EventPayload] = TypeAdapter(EventPayload)


def parse_event(data: str) -> EventPayload:
    return _EVENT_ADAPTER.validate_json(data)
