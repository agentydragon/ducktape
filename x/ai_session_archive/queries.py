"""Read-side queries shared by the archive importer and HTTP API."""

import base64
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session as DbSession

from x.ai_session_archive.database import (
    ResticSnapshotRow,
    FileVersionRecordRow,
    FileVersionRow,
    MachineRow,
    RecordPayloadRow,
    SessionRow,
    SnapshotFileRow,
)
from x.ai_session_archive.models import Event, EventPage, Harness, Machine, SessionPage, SessionSummary, SessionVersion

MAX_EVENT_PAGE_SIZE = 500
MAX_DELTA_DEPTH = 63


def list_machines(session: DbSession) -> list[Machine]:
    rows = session.scalars(select(MachineRow).order_by(MachineRow.id))
    return [Machine(id=row.id, display_name=row.display_name) for row in rows]


def _session_summary_query() -> Any:
    version_count = (
        select(func.count(FileVersionRow.id)).where(FileVersionRow.session_id == SessionRow.id).scalar_subquery()
    )
    latest_version_id = (
        select(SnapshotFileRow.file_version_id)
        .join(
            ResticSnapshotRow,
            (ResticSnapshotRow.machine_id == SnapshotFileRow.machine_id)
            & (ResticSnapshotRow.snapshot_id == SnapshotFileRow.snapshot_id),
        )
        .join(FileVersionRow, FileVersionRow.id == SnapshotFileRow.file_version_id)
        .where(FileVersionRow.session_id == SessionRow.id)
        .where(SnapshotFileRow.status.in_(("complete", "partial")))
        .order_by(ResticSnapshotRow.captured_at.desc(), ResticSnapshotRow.snapshot_id.desc())
        .limit(1)
        .scalar_subquery()
    )
    return select(SessionRow, version_count.label("version_count"), latest_version_id.label("latest_version_id"))


def _summary(row: Any) -> SessionSummary:
    session_row, version_count, latest_version_id = row
    return SessionSummary(
        id=session_row.id,
        machine_id=session_row.machine_id,
        harness=Harness(session_row.harness),
        title=session_row.title,
        cwd=session_row.cwd,
        parent_session_id=session_row.parent_session_id,
        parent_link_kind=session_row.parent_link_kind,
        identity_inferred=session_row.identity_inferred,
        started_at=session_row.started_at,
        last_activity_at=session_row.last_activity_at,
        version_count=version_count,
        latest_version_id=latest_version_id,
    )


def _decode_session_cursor(cursor: str) -> tuple[datetime | None, UUID]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        timestamp = datetime.fromisoformat(payload["last_activity_at"]) if payload["last_activity_at"] else None
        return timestamp, UUID(payload["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid session cursor") from exc


def _encode_session_cursor(row: SessionRow) -> str:
    payload = json.dumps(
        {"last_activity_at": row.last_activity_at.isoformat() if row.last_activity_at else None, "id": str(row.id)},
        separators=(",", ":"),
    ).encode("ascii")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def list_sessions(
    session: DbSession,
    *,
    machine_id: str | None,
    harness: Harness | None,
    parent_session_id: UUID | None = None,
    cursor: str | None,
    limit: int,
) -> SessionPage:
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")

    statement = _session_summary_query()
    if machine_id is not None:
        statement = statement.where(SessionRow.machine_id == machine_id)
    if harness is not None:
        statement = statement.where(SessionRow.harness == harness.value)
    if parent_session_id is not None:
        statement = statement.where(SessionRow.parent_session_id == parent_session_id)
    if cursor is not None:
        cursor_time, cursor_id = _decode_session_cursor(cursor)
        if cursor_time is None:
            statement = statement.where(SessionRow.last_activity_at.is_(None), SessionRow.id > cursor_id)
        else:
            statement = statement.where(
                or_(
                    SessionRow.last_activity_at < cursor_time,
                    SessionRow.last_activity_at.is_(None),
                    (SessionRow.last_activity_at == cursor_time) & (SessionRow.id > cursor_id),
                )
            )
    statement = statement.order_by(SessionRow.last_activity_at.desc().nullslast(), SessionRow.id).limit(limit + 1)
    rows = list(session.execute(statement))
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = _encode_session_cursor(page_rows[-1][0]) if has_more and page_rows else None
    return SessionPage(data=[_summary(row) for row in page_rows], next_cursor=next_cursor)


def get_session(session: DbSession, *, session_id: UUID) -> SessionSummary | None:
    row = session.execute(_session_summary_query().where(SessionRow.id == session_id)).first()
    return None if row is None else _summary(row)


def list_versions(session: DbSession, *, session_id: UUID) -> list[SessionVersion]:
    rows = session.execute(
        select(FileVersionRow)
        .where(FileVersionRow.session_id == session_id)
        .order_by(FileVersionRow.first_seen_at.desc(), FileVersionRow.id)
    ).scalars()
    return [
        SessionVersion(
            id=row.id,
            captured_at=row.first_seen_at,
            sha256=row.sha256,
            byte_size=row.byte_size,
            source_path=row.source_path,
            complete=row.complete,
            parse_issue_count=row.parse_issue_count,
            snapshot_id=row.first_snapshot_id,
        )
        for row in rows
    ]


def get_version(session: DbSession, *, session_id: UUID, version_id: UUID) -> SessionVersion | None:
    row = session.scalar(
        select(FileVersionRow).where(FileVersionRow.id == version_id, FileVersionRow.session_id == session_id)
    )
    if row is None:
        return None
    return SessionVersion(
        id=row.id,
        captured_at=row.first_seen_at,
        sha256=row.sha256,
        byte_size=row.byte_size,
        source_path=row.source_path,
        complete=row.complete,
        parse_issue_count=row.parse_issue_count,
        snapshot_id=row.first_snapshot_id,
    )


def _version_lineage(session: DbSession, version: FileVersionRow) -> list[FileVersionRow]:
    lineage = [version]
    while not lineage[-1].is_checkpoint:
        if len(lineage) > MAX_DELTA_DEPTH:
            raise RuntimeError(f"file version delta chain exceeds {MAX_DELTA_DEPTH}: {version.id}")
        parent_id = lineage[-1].parent_version_id
        if parent_id is None:
            raise RuntimeError(f"non-checkpoint file version has no parent: {lineage[-1].id}")
        parent = session.get(FileVersionRow, parent_id)
        if parent is None or parent.session_id != version.session_id:
            raise RuntimeError(f"file version has an invalid parent: {lineage[-1].id}")
        lineage.append(parent)
    return lineage


def list_events(
    session: DbSession, *, session_id: UUID, version_id: UUID, cursor: int | None, limit: int
) -> EventPage | None:
    if not 1 <= limit <= MAX_EVENT_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_EVENT_PAGE_SIZE}")
    version = session.scalar(
        select(FileVersionRow).where(FileVersionRow.id == version_id, FileVersionRow.session_id == session_id)
    )
    if version is None:
        return None

    if cursor is not None and cursor < 0:
        raise ValueError("cursor must be a non-negative record ordinal")
    start = 0 if cursor is None else cursor + 1
    end = min(version.record_count, start + limit + 1)
    if start >= end:
        return EventPage(data=[], next_cursor=None)

    lineage = _version_lineage(session, version)
    lineage_rank = {row.id: index for index, row in enumerate(lineage)}
    rows = session.execute(
        select(FileVersionRecordRow, RecordPayloadRow)
        .join(RecordPayloadRow, RecordPayloadRow.id == FileVersionRecordRow.payload_id)
        .where(
            FileVersionRecordRow.file_version_id.in_(lineage_rank),
            FileVersionRecordRow.ordinal >= start,
            FileVersionRecordRow.ordinal < end,
        )
        .order_by(FileVersionRecordRow.ordinal, FileVersionRecordRow.file_version_id)
    )
    resolved: dict[int, tuple[int, FileVersionRecordRow, RecordPayloadRow]] = {}
    for record, payload in rows:
        rank = lineage_rank[record.file_version_id]
        current = resolved.get(record.ordinal)
        if current is None or rank < current[0]:
            resolved[record.ordinal] = (rank, record, payload)

    result_rows = [resolved[ordinal] for ordinal in range(start, end) if ordinal in resolved]
    expected_count = min(limit + 1, version.record_count - start)
    if len(result_rows) != expected_count:
        raise RuntimeError(f"file version {version_id} is missing stored records in ordinals {start}:{end}")

    has_more = len(result_rows) > limit
    page_rows = result_rows[:limit]
    events = [
        Event(
            ordinal=record.ordinal,
            byte_offset=record.byte_offset,
            timestamp=payload.timestamp,
            kind=payload.kind,
            parsed=payload.parsed,
            parse_error=payload.parse_error,
            raw=payload.raw_bytes.decode("utf-8", errors="replace").rstrip("\r\n"),
            raw_base64=base64.b64encode(payload.raw_bytes).decode("ascii"),
        )
        for _, record, payload in page_rows
    ]
    next_cursor = events[-1].ordinal if has_more and events else None
    return EventPage(data=events, next_cursor=next_cursor)
