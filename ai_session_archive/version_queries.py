"""Read captured versions and their transcript records."""

import base64
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ai_session_archive.database import FileVersionRecordRow, FileVersionRow, RecordPayloadRow
from ai_session_archive.models import Event, EventDirection, EventPage, SessionVersion
from ai_session_archive.record_json import ParsedRecordJson, RecordJsonParseError, parse_record_json
from ai_session_archive.version_lineage import CHECKPOINT_INTERVAL, version_lineage

MAX_EVENT_PAGE_SIZE = 500
MAX_DELTA_DEPTH = CHECKPOINT_INTERVAL - 1


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


def list_events(
    session: DbSession,
    *,
    session_id: UUID,
    version_id: UUID,
    cursor: int | None,
    limit: int,
    direction: EventDirection = EventDirection.FORWARD,
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

    if direction is EventDirection.BACKWARD:
        end = version.record_count if cursor is None else min(cursor, version.record_count)
        start = max(0, end - limit - 1)
    else:
        start = 0 if cursor is None else cursor + 1
        end = min(version.record_count, start + limit + 1)
    if start >= end:
        return EventPage(data=[], next_cursor=None)

    lineage = version_lineage(session, version, max_delta_depth=MAX_DELTA_DEPTH)
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
    if len(result_rows) != end - start:
        raise RuntimeError(f"file version {version_id} is missing stored records in ordinals {start}:{end}")

    has_more = len(result_rows) > limit
    page_rows = result_rows[-limit:] if direction is EventDirection.BACKWARD else result_rows[:limit]
    events = []
    for _, record, payload in page_rows:
        parsed = parse_record_json(payload.raw_bytes)
        events.append(
            Event(
                ordinal=record.ordinal,
                byte_offset=record.byte_offset,
                timestamp=payload.timestamp,
                kind=payload.kind,
                parsed=parsed.parsed if isinstance(parsed, ParsedRecordJson) else None,
                parse_error=parsed.parse_error if isinstance(parsed, RecordJsonParseError) else None,
                raw=payload.raw_bytes.decode("utf-8", errors="replace").rstrip("\r\n"),
                raw_base64=base64.b64encode(payload.raw_bytes).decode("ascii"),
            )
        )
    if has_more and events:
        next_cursor = events[0].ordinal if direction is EventDirection.BACKWARD else events[-1].ordinal
    else:
        next_cursor = None
    return EventPage(data=events, next_cursor=next_cursor)
