"""Read-side queries shared by the archive importer and HTTP API."""

import base64
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import exists, false, func, or_, select
from sqlalchemy.orm import Session as DbSession

from ai_session_archive.database import FileVersionRow, ResticSnapshotRow, SessionRow, SnapshotFileRow
from ai_session_archive.models import Harness, SessionKind, SessionPage, SessionSummary, TranscriptStatus
from ai_session_archive.statuses import SnapshotFileStatus


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
        .where(SnapshotFileRow.status.in_((SnapshotFileStatus.COMPLETE, SnapshotFileStatus.PARTIAL)))
        .order_by(ResticSnapshotRow.captured_at.desc(), ResticSnapshotRow.snapshot_id.desc())
        .limit(1)
        .scalar_subquery()
    )
    latest_version_record_count = (
        select(FileVersionRow.record_count)
        .join(SnapshotFileRow, SnapshotFileRow.file_version_id == FileVersionRow.id)
        .join(
            ResticSnapshotRow,
            (ResticSnapshotRow.machine_id == SnapshotFileRow.machine_id)
            & (ResticSnapshotRow.snapshot_id == SnapshotFileRow.snapshot_id),
        )
        .where(FileVersionRow.session_id == SessionRow.id)
        .where(SnapshotFileRow.status.in_((SnapshotFileStatus.COMPLETE, SnapshotFileStatus.PARTIAL)))
        .order_by(ResticSnapshotRow.captured_at.desc(), ResticSnapshotRow.snapshot_id.desc())
        .limit(1)
        .scalar_subquery()
    )
    has_pending_file = exists(
        select(1).where(
            SnapshotFileRow.session_id == SessionRow.id, SnapshotFileRow.status == SnapshotFileStatus.PENDING
        )
    )
    has_failed_file = exists(
        select(1).where(
            SnapshotFileRow.session_id == SessionRow.id, SnapshotFileRow.status == SnapshotFileStatus.FAILED
        )
    )
    return select(
        SessionRow,
        version_count.label("version_count"),
        latest_version_id.label("latest_version_id"),
        latest_version_record_count.label("latest_version_record_count"),
        has_pending_file.label("has_pending_file"),
        has_failed_file.label("has_failed_file"),
    )


def _summary(row: Any) -> SessionSummary:
    session_row, version_count, latest_version_id, latest_version_record_count, has_pending_file, has_failed_file = row
    if version_count > 0:
        transcript_status = TranscriptStatus.CAPTURED
    elif has_pending_file:
        transcript_status = TranscriptStatus.PENDING
    elif has_failed_file:
        transcript_status = TranscriptStatus.FAILED
    else:
        transcript_status = TranscriptStatus.UNAVAILABLE
    return SessionSummary(
        id=session_row.id,
        machine_id=session_row.machine_id,
        harness=Harness(session_row.harness),
        session_kind=SessionKind(session_row.session_kind or SessionKind.UNKNOWN.value),
        title=session_row.title,
        cwd=session_row.cwd,
        parent_session_id=session_row.parent_session_id,
        parent_native_session_id=session_row.parent_native_session_id,
        parent_link_kind=session_row.parent_link_kind,
        identity_inferred=session_row.identity_inferred,
        native_session_id_conflict=session_row.native_session_id_conflict,
        native_session_id_candidates=session_row.native_session_id_candidates,
        parent_native_session_id_conflict=session_row.parent_native_session_id_conflict,
        parent_native_session_id_candidates=session_row.parent_native_session_id_candidates,
        started_at=session_row.started_at,
        last_activity_at=session_row.last_activity_at,
        version_count=version_count,
        latest_version_id=latest_version_id,
        latest_version_record_count=latest_version_record_count,
        transcript_status=transcript_status,
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
    kinds: list[SessionKind] | None = None,
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
    if kinds is not None:
        kind_values = [kind.value for kind in kinds]
        if not kind_values:
            statement = statement.where(false())
        elif SessionKind.UNKNOWN in kinds:
            statement = statement.where(
                or_(SessionRow.session_kind.in_(kind_values), SessionRow.session_kind.is_(None))
            )
        else:
            statement = statement.where(SessionRow.session_kind.in_(kind_values))
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
