# gazelle:include_dep @pypi//psycopg
"""PostgreSQL schema and connection helpers for the session archive."""

from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
    create_engine,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


JsonType = JSON().with_variant(JSONB(), "postgresql")


class MachineRow(Base):
    __tablename__ = "machines"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)


class ResticSnapshotRow(Base):
    __tablename__ = "restic_snapshots"

    machine_id: Mapped[str] = mapped_column(ForeignKey("machines.id", ondelete="RESTRICT"), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    parent_snapshot_id: Mapped[str | None] = mapped_column(String(64))
    tree_id: Mapped[str | None] = mapped_column(String(64))
    paths: Mapped[list[str]] = mapped_column(JsonType, nullable=False)
    comparison_snapshot_id: Mapped[str | None] = mapped_column(String(64))
    inventory_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    inventory_error: Mapped[str | None] = mapped_column(String)
    processing_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # Restic snapshot metadata does not establish whether the workstation capture skipped unreadable files.
    capture_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    __table_args__ = (
        CheckConstraint(
            "inventory_status IN ('pending', 'complete', 'failed')", name="ck_restic_snapshots_inventory_status"
        ),
        CheckConstraint(
            "processing_status IN ('pending', 'complete', 'failed')", name="ck_restic_snapshots_processing_status"
        ),
        CheckConstraint(
            "capture_status IN ('unknown', 'complete', 'incomplete')", name="ck_restic_snapshots_capture_status"
        ),
        Index("idx_restic_snapshots_machine_time", "machine_id", "captured_at"),
    )


class SessionRow(Base):
    __tablename__ = "sessions"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    machine_id: Mapped[str] = mapped_column(ForeignKey("machines.id", ondelete="RESTRICT"), nullable=False)
    harness: Mapped[str] = mapped_column(String(20), nullable=False)
    source_path: Mapped[str] = mapped_column(String, nullable=False)
    source_relative_path: Mapped[str] = mapped_column(String, nullable=False)
    native_session_id: Mapped[str | None] = mapped_column(String)
    native_session_id_inferred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    native_session_id_conflict: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    native_session_id_candidates: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    parent_session_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    parent_native_session_id: Mapped[str | None] = mapped_column(String)
    parent_native_session_id_conflict: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    parent_native_session_id_candidates: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    parent_link_kind: Mapped[str | None] = mapped_column(String(40))
    identity_inferred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    title: Mapped[str | None] = mapped_column(String)
    cwd: Mapped[str | None] = mapped_column(String)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("harness IN ('claude', 'codex')", name="ck_sessions_harness"),
        UniqueConstraint("machine_id", "harness", "source_path", name="uq_sessions_source_path"),
        Index("idx_sessions_machine_harness_activity", "machine_id", "harness", "last_activity_at", "id"),
        Index("idx_sessions_machine_harness_native", "machine_id", "harness", "native_session_id"),
        Index("idx_sessions_machine_harness_conflict", "machine_id", "harness", "native_session_id_conflict"),
    )


class FileVersionRow(Base):
    __tablename__ = "file_versions"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id", ondelete="RESTRICT"), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_path: Mapped[str] = mapped_column(String, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    parse_issue_count: Mapped[int] = mapped_column(Integer, nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("file_versions.id", ondelete="RESTRICT"))
    shared_prefix_record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_checkpoint: Mapped[bool] = mapped_column(Boolean, nullable=False)
    delta_depth: Mapped[int] = mapped_column(Integer, nullable=False)
    parser_version: Mapped[str] = mapped_column(String(50), nullable=False)
    __table_args__ = (
        UniqueConstraint("session_id", "sha256", name="uq_file_versions_session_sha256"),
        CheckConstraint("byte_size >= 0", name="ck_file_versions_byte_size"),
        CheckConstraint("parse_issue_count >= 0", name="ck_file_versions_parse_issue_count"),
        CheckConstraint("record_count >= 0", name="ck_file_versions_record_count"),
        CheckConstraint("delta_depth >= 0", name="ck_file_versions_delta_depth"),
        Index("idx_file_versions_session_seen", "session_id", "first_seen_at", "id"),
    )


class SnapshotFileRow(Base):
    __tablename__ = "snapshot_files"

    machine_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[UUID | None] = mapped_column(ForeignKey("sessions.id", ondelete="RESTRICT"))
    change_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    file_type: Mapped[str] = mapped_column(String(30), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    file_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("file_versions.id", ondelete="RESTRICT"))
    error: Mapped[str | None] = mapped_column(String)
    __table_args__ = (
        ForeignKeyConstraint(
            ["machine_id", "snapshot_id"],
            ["restic_snapshots.machine_id", "restic_snapshots.snapshot_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("byte_size >= 0", name="ck_snapshot_files_byte_size"),
        CheckConstraint(
            "status IN ('pending', 'not_indexed', 'unclassified', 'deleted', 'complete', 'partial', 'failed')",
            name="ck_snapshot_files_status",
        ),
        Index("idx_snapshot_files_retry", "status", "machine_id", "snapshot_id"),
        Index("idx_snapshot_files_version", "file_version_id"),
    )


class SessionRootRow(Base):
    __tablename__ = "session_roots"

    machine_id: Mapped[str] = mapped_column(ForeignKey("machines.id", ondelete="RESTRICT"), primary_key=True)
    harness: Mapped[str] = mapped_column(String(20), primary_key=True)
    root_path: Mapped[str] = mapped_column(String, primary_key=True)
    __table_args__ = (CheckConstraint("harness IN ('claude', 'codex')", name="ck_session_roots_harness"),)


class RecordPayloadRow(Base):
    __tablename__ = "record_payloads"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(50), nullable=False)
    raw_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    parsed: Mapped[Any | None] = mapped_column(JsonType)
    parse_error: Mapped[str | None] = mapped_column(String)
    timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str | None] = mapped_column(String)
    __table_args__ = (UniqueConstraint("sha256", "parser_version", name="uq_record_payloads_sha_parser"),)


class FileVersionRecordRow(Base):
    __tablename__ = "file_version_records"

    file_version_id: Mapped[UUID] = mapped_column(ForeignKey("file_versions.id", ondelete="RESTRICT"), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    byte_offset: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload_id: Mapped[UUID] = mapped_column(ForeignKey("record_payloads.id", ondelete="RESTRICT"), nullable=False)
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ck_file_version_records_ordinal"),
        CheckConstraint("byte_offset >= 0", name="ck_file_version_records_offset"),
        Index("idx_file_version_records_payload", "payload_id"),
    )


def make_engine(dsn: str, **kwargs: Any) -> Engine:
    return create_engine(dsn, pool_pre_ping=True, **kwargs)


def make_session_factory(engine: Engine) -> Callable[[], Session]:
    return sessionmaker(engine, expire_on_commit=False)


def initialize_schema(engine: Engine) -> None:
    """Create the initial schema for a new archive database."""
    Base.metadata.create_all(engine)
