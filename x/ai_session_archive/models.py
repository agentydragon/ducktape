"""Read API models for archived Claude Code and Codex transcript versions."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class Harness(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"


class Machine(BaseModel):
    id: str
    display_name: str


class SessionSummary(BaseModel):
    id: UUID
    machine_id: str
    harness: Harness
    title: str | None
    cwd: str | None
    parent_session_id: UUID | None = None
    parent_link_kind: str | None = None
    identity_inferred: bool = False
    started_at: datetime | None
    last_activity_at: datetime | None
    version_count: int
    latest_version_id: UUID | None


class SessionVersion(BaseModel):
    id: UUID
    captured_at: datetime
    sha256: str
    byte_size: int
    source_path: str
    complete: bool
    parse_issue_count: int
    snapshot_id: str


class Event(BaseModel):
    ordinal: int
    byte_offset: int
    timestamp: datetime | None
    kind: str | None
    parsed: Any | None
    parse_error: str | None
    raw: str
    raw_base64: str


class SessionPage(BaseModel):
    data: list[SessionSummary]
    next_cursor: str | None


class EventPage(BaseModel):
    data: list[Event]
    next_cursor: int | None
