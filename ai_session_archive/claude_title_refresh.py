"""Refresh stale Claude title projections from stored record payloads."""

from collections.abc import Callable, Mapping
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.orm import Session as DbSession

from ai_session_archive.claude_titles import ClaudeTitle, select_claude_title
from ai_session_archive.database import FileVersionRow, RecordPayloadRow, ResticSnapshotRow, SessionRow, SnapshotFileRow
from ai_session_archive.models import Harness
from ai_session_archive.record_json import ParsedRecordJson, parse_record_json
from ai_session_archive.session_projection import CLAUDE_TITLE_PROJECTION_VERSION
from ai_session_archive.statuses import SnapshotFileStatus
from ai_session_archive.version_lineage import effective_payload_ids, version_lineage_ranks


def refresh_claude_titles(session_factory: Callable[[], DbSession], machine_id: str) -> None:
    latest_version_id = (
        select(SnapshotFileRow.file_version_id)
        .join(
            ResticSnapshotRow,
            and_(
                ResticSnapshotRow.machine_id == SnapshotFileRow.machine_id,
                ResticSnapshotRow.snapshot_id == SnapshotFileRow.snapshot_id,
            ),
        )
        .where(
            SnapshotFileRow.session_id == SessionRow.id,
            SnapshotFileRow.status.in_((SnapshotFileStatus.COMPLETE, SnapshotFileStatus.PARTIAL)),
            SnapshotFileRow.file_version_id.is_not(None),
        )
        .order_by(ResticSnapshotRow.captured_at.desc(), ResticSnapshotRow.snapshot_id.desc())
        .limit(1)
        .scalar_subquery()
    )
    with session_factory() as db, db.begin():
        rows = db.execute(
            select(SessionRow, FileVersionRow)
            .join(FileVersionRow, FileVersionRow.id == latest_version_id)
            .where(
                SessionRow.machine_id == machine_id,
                SessionRow.harness == Harness.CLAUDE.value,
                (SessionRow.claude_title_projection_version.is_(None))
                | (SessionRow.claude_title_projection_version != CLAUDE_TITLE_PROJECTION_VERSION),
            )
            .order_by(SessionRow.id)
        )
        for session_row, version_row in rows.yield_per(8):
            title: ClaudeTitle | None = None
            ranks = version_lineage_ranks(db, version_row)
            for start in range(0, version_row.record_count, 500):
                batch_ids = effective_payload_ids(
                    db, version_row, ranks, start, min(start + 500, version_row.record_count)
                )
                raw_by_id: dict[UUID, bytes] = dict(
                    db.execute(
                        select(RecordPayloadRow.id, RecordPayloadRow.raw_bytes).where(
                            RecordPayloadRow.id.in_(batch_ids)
                        )
                    )
                    .tuples()
                    .all()
                )
                for payload_id in batch_ids:
                    parsed = parse_record_json(raw_by_id[payload_id])
                    if isinstance(parsed, ParsedRecordJson) and isinstance(parsed.parsed, Mapping):
                        title = select_claude_title(title, parsed.parsed)
            session_row.title = title.text if title is not None else None
            session_row.claude_title_projection_version = CLAUDE_TITLE_PROJECTION_VERSION
