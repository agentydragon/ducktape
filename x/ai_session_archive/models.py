"""Read API models for archived Claude Code and Codex transcript versions."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class Harness(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"


class TranscriptStatus(StrEnum):
    CAPTURED = "captured"
    PENDING = "pending"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class EventDirection(StrEnum):
    FORWARD = "forward"
    BACKWARD = "backward"


class Machine(BaseModel):
    id: str
    display_name: str


class SnapshotImportState(StrEnum):
    INVENTORY_PENDING = "inventory_pending"
    INVENTORY_FAILED = "inventory_failed"
    CODEX_METADATA_PENDING = "codex_metadata_pending"
    CODEX_METADATA_FAILED = "codex_metadata_failed"
    PROCESSING_PENDING = "processing_pending"
    PROCESSING_FAILED = "processing_failed"
    COMPLETE = "complete"


class MachineImportProgress(BaseModel):
    machine_id: str
    display_name: str
    known_snapshot_count: int = 0
    completed_snapshot_count: int = 0
    failed_snapshot_count: int = 0
    codex_metadata_pending_snapshot_count: int = 0
    codex_metadata_failed_snapshot_count: int = 0
    codex_metadata_partial_snapshot_count: int = 0
    oldest_unfinished_snapshot_at: datetime | None = None
    oldest_unfinished_snapshot_state: SnapshotImportState | None = None
    processed_file_count: int = 0
    pending_file_count: int = 0
    failed_file_count: int = 0


class SessionSummary(BaseModel):
    id: UUID
    machine_id: str
    harness: Harness
    title: str | None
    cwd: str | None
    parent_session_id: UUID | None = None
    parent_native_session_id: str | None = None
    parent_link_kind: str | None = None
    identity_inferred: bool = False
    native_session_id_conflict: bool = False
    native_session_id_candidates: list[str] = Field(default_factory=list)
    parent_native_session_id_conflict: bool = False
    parent_native_session_id_candidates: list[str] = Field(default_factory=list)
    started_at: datetime | None
    last_activity_at: datetime | None
    version_count: int
    latest_version_id: UUID | None
    latest_version_record_count: int | None = Field(
        description="JSONL records in the latest captured version, or null when no latest version is linked."
    )
    transcript_status: TranscriptStatus = Field(
        description="Availability derived from captured versions and inventoried file processing state."
    )


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
