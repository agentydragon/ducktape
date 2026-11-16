"""Stored status vocabularies for the snapshot and file ledger."""

from enum import StrEnum


class SnapshotInventoryStatus(StrEnum):
    PENDING = "pending"
    COMPLETE = "complete"
    FAILED = "failed"


class SnapshotProcessingStatus(StrEnum):
    PENDING = "pending"
    COMPLETE = "complete"
    FAILED = "failed"


class CodexMetadataStatus(StrEnum):
    PENDING = "pending"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class SnapshotCaptureStatus(StrEnum):
    UNKNOWN = "unknown"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class SnapshotFileStatus(StrEnum):
    PENDING = "pending"
    NOT_INDEXED = "not_indexed"
    UNCLASSIFIED = "unclassified"
    DELETED = "deleted"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
