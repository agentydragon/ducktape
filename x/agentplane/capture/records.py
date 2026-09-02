"""Pydantic shapes for the persisted upstream model exchange, not native provider frames."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RequestRecord(BaseModel):
    kind: Literal["request"]
    capture_request_id: str
    method: Literal["POST"]
    path_query: str
    body: str
    time_ns: int | None = Field(default=None, ge=0)


class ResponseChunkRecord(BaseModel):
    kind: Literal["response_chunk"]
    capture_request_id: str
    ordinal: int = Field(ge=1)
    body: str
    time_ns: int | None = Field(default=None, ge=0)


class ConnectionDroppedRecord(BaseModel):
    """One intentional stream loss at the recorded native-response boundary."""

    kind: Literal["connection_dropped"]
    capture_request_id: str
    after_event: str = Field(min_length=1)
    time_ns: int | None = Field(default=None, ge=0)


class ProxyErrorRecord(BaseModel):
    kind: Literal["proxy_error"]
    capture_request_id: str
    error_kind: str


class CaptureMetadata(BaseModel):
    provider: Literal["claude", "codex"]
    scenario: str
    model: str
