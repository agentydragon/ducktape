"""Atomically publish a staged transcript version and its session projection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import case, delete, insert, literal, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session as DbSession

from ai_session_archive.database import (
    FileVersionImportRecordRow,
    FileVersionImportRow,
    FileVersionRecordRow,
    FileVersionRow,
    ResticSnapshotRow,
    SessionRow,
    SnapshotFileRow,
)
from ai_session_archive.record_payloads import PARSER_VERSION
from ai_session_archive.restic import ResticSnapshot
from ai_session_archive.session_projection import update_session_metadata
from ai_session_archive.snapshot_inventory import assert_snapshot_identity
from ai_session_archive.source_paths import SourceLocation
from ai_session_archive.statuses import SnapshotFileStatus
from ai_session_archive.version_lineage import CHECKPOINT_INTERVAL, version_lineage_ranks


@dataclass(frozen=True)
class SnapshotFileIdentity:
    change_kind: str
    file_type: str
    byte_size: int
    modified_at: datetime | None
    session_id: UUID | None

    @classmethod
    def from_row(cls, row: SnapshotFileRow) -> SnapshotFileIdentity:
        return cls(
            change_kind=row.change_kind,
            file_type=row.file_type,
            byte_size=row.byte_size,
            modified_at=row.modified_at,
            session_id=row.session_id,
        )


def previous_version(db: DbSession, current: SnapshotFileRow) -> FileVersionRow | None:
    # A path absent from many consecutive diffs must not cost one database round trip per snapshot.
    # Stop at the first independent inventory, matching the diff ancestry's trust boundary.
    # The recursion probes one parent per ancestor with LATERAL ... LIMIT 1: a plain join scanned every machine
    # snapshot at each step (445 snapshots, 9,108 shared buffer hits on wyrm2 for one path).
    prior = db.execute(
        text(
            """
            WITH RECURSIVE ancestors AS (
                SELECT snapshot_id, parent_snapshot_id, comparison_snapshot_id,
                       ARRAY[snapshot_id]::varchar[] AS seen, 0 AS depth
                FROM restic_snapshots
                WHERE machine_id = :machine_id
                  AND snapshot_id = (
                      SELECT comparison_snapshot_id FROM restic_snapshots
                      WHERE machine_id = :machine_id AND snapshot_id = :snapshot_id
                  )
                UNION ALL
                SELECT parent.snapshot_id, parent.parent_snapshot_id, parent.comparison_snapshot_id,
                       ancestors.seen || parent.snapshot_id, ancestors.depth + 1
                FROM ancestors
                JOIN LATERAL (
                    SELECT snapshot_id, parent_snapshot_id, comparison_snapshot_id
                    FROM restic_snapshots
                    WHERE machine_id = :machine_id AND snapshot_id = ancestors.parent_snapshot_id
                    LIMIT 1
                ) AS parent ON true
                WHERE ancestors.comparison_snapshot_id IS NOT NULL
                  AND NOT parent.snapshot_id = ANY(ancestors.seen)
            )
            SELECT prior.status, prior.file_version_id
            FROM ancestors
            JOIN snapshot_files AS prior
              ON prior.machine_id = :machine_id
             AND prior.snapshot_id = ancestors.snapshot_id
             AND prior.path = :path
            ORDER BY ancestors.depth
            LIMIT 1
            """
        ),
        {"machine_id": current.machine_id, "snapshot_id": current.snapshot_id, "path": current.path},
    ).first()
    if (
        prior is None
        or prior.status not in (SnapshotFileStatus.COMPLETE, SnapshotFileStatus.PARTIAL)
        or prior.file_version_id is None
    ):
        return None
    return db.get(FileVersionRow, prior.file_version_id)


def publish_file_version(
    session_factory: Callable[[], DbSession],
    machine_id: str,
    *,
    snapshot: ResticSnapshot,
    path: str,
    location: SourceLocation,
    version_id: UUID,
    session_id: UUID,
    sha256: str,
    byte_size: int,
    record_count: int,
    metadata: dict[str, Any],
    file_identity: SnapshotFileIdentity,
    inventory_status: str,
    comparison_snapshot_id: str | None,
) -> None:
    with session_factory() as db, db.begin():
        snapshot_row = db.get(ResticSnapshotRow, (machine_id, snapshot.id), with_for_update=True)
        if snapshot_row is None:
            raise RuntimeError(f"snapshot row disappeared for {snapshot.id}")
        assert_snapshot_identity(snapshot_row, snapshot)
        snapshot_file = db.get(SnapshotFileRow, (machine_id, snapshot.id, path), with_for_update=True)
        if snapshot_file is None or snapshot_file.status not in (SnapshotFileStatus.PENDING, SnapshotFileStatus.FAILED):
            return
        if SnapshotFileIdentity.from_row(snapshot_file) != file_identity:
            raise RuntimeError(f"snapshot file identity changed while parsing {path}")
        if (
            snapshot_row.inventory_status != inventory_status
            or snapshot_row.comparison_snapshot_id != comparison_snapshot_id
        ):
            return
        session_row = db.get(SessionRow, session_id, with_for_update=True)
        if session_row is None:
            raise RuntimeError(f"missing session row for {path}")
        stage = db.get(FileVersionImportRow, version_id, with_for_update=True)
        existing = db.scalar(
            select(FileVersionRow).where(FileVersionRow.session_id == session_id, FileVersionRow.sha256 == sha256)
        )
        if existing is None:
            if stage is None or stage.imported_record_count != record_count:
                raise RuntimeError(f"file import {version_id} is unfinished")
            previous = previous_version(db, snapshot_file)
            # An independently progressing ancestor may have appeared while we parsed.
            # Reconstruct a checkpoint from the retained parent if the current ancestry changed.
            use_parent = (
                previous is not None
                and previous.id == stage.parent_version_id
                and previous.delta_depth + 1 < CHECKPOINT_INTERVAL
            )
            prefix = stage.shared_prefix_record_count if use_parent else 0
            inserted = db.scalar(
                pg_insert(FileVersionRow)
                .values(
                    id=version_id,
                    session_id=session_id,
                    sha256=sha256,
                    byte_size=byte_size,
                    source_path=location.source_path,
                    first_seen_at=snapshot.captured_at,
                    last_seen_at=snapshot.captured_at,
                    first_snapshot_id=snapshot.id,
                    complete=stage.parse_issue_count == 0,
                    parse_issue_count=stage.parse_issue_count,
                    record_count=record_count,
                    parent_version_id=previous.id if use_parent and previous else None,
                    shared_prefix_record_count=prefix,
                    is_checkpoint=not use_parent,
                    delta_depth=previous.delta_depth + 1 if use_parent and previous else 0,
                    parser_version=PARSER_VERSION,
                )
                .on_conflict_do_nothing()
                .returning(FileVersionRow.id)
            )
            if inserted is not None:
                # Publication copies only compact mappings, but its transaction scales with the suffix.
                if not use_parent and stage.shared_prefix_record_count:
                    staged_parent = db.get(FileVersionRow, stage.parent_version_id)
                    if staged_parent is None or staged_parent.session_id != session_id:
                        raise RuntimeError(f"invalid staged parent for {version_id}")
                    ranks = version_lineage_ranks(db, staged_parent)
                    db.execute(
                        insert(FileVersionRecordRow).from_select(
                            ["file_version_id", "ordinal", "byte_offset", "payload_id"],
                            select(
                                literal(version_id),
                                FileVersionRecordRow.ordinal,
                                FileVersionRecordRow.byte_offset,
                                FileVersionRecordRow.payload_id,
                            )
                            .where(
                                FileVersionRecordRow.file_version_id.in_(ranks),
                                FileVersionRecordRow.ordinal < stage.shared_prefix_record_count,
                            )
                            .distinct(FileVersionRecordRow.ordinal)
                            .order_by(
                                FileVersionRecordRow.ordinal, case(ranks, value=FileVersionRecordRow.file_version_id)
                            ),
                        )
                    )
                db.execute(
                    insert(FileVersionRecordRow).from_select(
                        ["file_version_id", "ordinal", "byte_offset", "payload_id"],
                        select(
                            literal(version_id),
                            FileVersionImportRecordRow.ordinal,
                            FileVersionImportRecordRow.byte_offset,
                            FileVersionImportRecordRow.payload_id,
                        ).where(
                            FileVersionImportRecordRow.import_id == version_id,
                            FileVersionImportRecordRow.ordinal >= prefix,
                        ),
                    )
                )
            else:
                metadata = {}
            existing = db.scalar(
                select(FileVersionRow).where(FileVersionRow.session_id == session_id, FileVersionRow.sha256 == sha256)
            )
            assert existing is not None
        else:
            metadata = {}
        if existing.byte_size != byte_size or existing.parser_version != PARSER_VERSION:
            raise RuntimeError("file content hash collides with an incompatible stored version")
        if (snapshot.captured_at, snapshot.id) < (existing.first_seen_at, existing.first_snapshot_id):
            existing.first_seen_at = snapshot.captured_at
            existing.first_snapshot_id = snapshot.id
        existing.last_seen_at = max(existing.last_seen_at, snapshot.captured_at)
        snapshot_file.file_version_id = existing.id
        snapshot_file.byte_size = byte_size
        snapshot_file.file_type = "file"
        snapshot_file.status = SnapshotFileStatus.COMPLETE if existing.complete else SnapshotFileStatus.PARTIAL
        snapshot_file.error = None
        update_session_metadata(db, session_row, metadata)
        if stage is not None:
            db.execute(delete(FileVersionImportRow).where(FileVersionImportRow.id == version_id))
