"""Follow published transcript versions back to their checkpoint."""

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ai_session_archive.database import FileVersionRecordRow, FileVersionRow

CHECKPOINT_INTERVAL = 64


def version_lineage(db: DbSession, version: FileVersionRow, *, max_delta_depth: int) -> list[FileVersionRow]:
    """Return newest-first versions, rejecting missing or cross-session parents."""
    lineage = [version]
    while not lineage[-1].is_checkpoint:
        parent_id = lineage[-1].parent_version_id
        if len(lineage) > max_delta_depth:
            raise RuntimeError(f"file version delta chain exceeds {max_delta_depth}: {version.id}")
        if parent_id is None:
            raise RuntimeError(f"non-checkpoint file version has no parent: {lineage[-1].id}")
        parent = db.get(FileVersionRow, parent_id)
        if parent is None or parent.session_id != version.session_id:
            raise RuntimeError(f"file version has an invalid parent: {lineage[-1].id}")
        lineage.append(parent)
    return lineage


def effective_payload_ids(
    db: DbSession, version: FileVersionRow, ranks: Mapping[UUID, int], start: int, end: int
) -> list[UUID]:
    end = min(end, version.record_count)
    if start >= end:
        return []
    rows = db.execute(
        select(FileVersionRecordRow.file_version_id, FileVersionRecordRow.ordinal, FileVersionRecordRow.payload_id)
        .where(FileVersionRecordRow.file_version_id.in_(ranks))
        .where(FileVersionRecordRow.ordinal >= start, FileVersionRecordRow.ordinal < end)
        .order_by(FileVersionRecordRow.ordinal, FileVersionRecordRow.file_version_id)
    )
    resolved: dict[int, tuple[int, UUID]] = {}
    for version_id, ordinal, payload_id in rows:
        rank = ranks[version_id]
        if ordinal not in resolved or rank < resolved[ordinal][0]:
            resolved[ordinal] = (rank, payload_id)
    result = []
    for ordinal in range(start, end):
        if ordinal not in resolved:
            raise RuntimeError(f"file version {version.id} is missing record {ordinal}")
        result.append(resolved[ordinal][1])
    return result


def version_lineage_ranks(db: DbSession, version: FileVersionRow) -> dict[UUID, int]:
    # Preserve the importer's existing limit, one edge more than the read API's.
    return {row.id: rank for rank, row in enumerate(version_lineage(db, version, max_delta_depth=CHECKPOINT_INTERVAL))}
