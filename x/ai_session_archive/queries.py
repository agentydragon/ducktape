"""Read-side queries shared by the archive importer and HTTP API."""

import base64
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session as DbSession

from x.ai_session_archive.database import (
    FileVersionRecordRow,
    FileVersionRow,
    MachineRow,
    RecordPayloadRow,
    ResticSnapshotRow,
    SessionRow,
    SnapshotFileRow,
)
from x.ai_session_archive.models import (
    Event,
    EventPage,
    Harness,
    Machine,
    MachineImportProgress,
    SessionPage,
    SessionSummary,
    SessionVersion,
    SnapshotImportState,
    TranscriptStatus,
)
from x.ai_session_archive.record_json import parse_record_json

MAX_EVENT_PAGE_SIZE = 500
MAX_DELTA_DEPTH = 63


def list_machines(session: DbSession) -> list[Machine]:
    rows = session.scalars(select(MachineRow).order_by(MachineRow.id))
    return [Machine(id=row.id, display_name=row.display_name) for row in rows]


def list_import_progress(session: DbSession) -> list[MachineImportProgress]:
    machines = list(session.scalars(select(MachineRow).order_by(MachineRow.id)))
    progress = {
        machine.id: MachineImportProgress(machine_id=machine.id, display_name=machine.display_name)
        for machine in machines
    }
    snapshots_by_machine: dict[str, list[tuple[datetime, SnapshotImportState]]] = {}
    for machine_id, captured_at, inventory_status, processing_status, codex_metadata_status in session.execute(
        select(
            ResticSnapshotRow.machine_id,
            ResticSnapshotRow.captured_at,
            ResticSnapshotRow.inventory_status,
            ResticSnapshotRow.processing_status,
            ResticSnapshotRow.codex_metadata_status,
        ).order_by(ResticSnapshotRow.machine_id, ResticSnapshotRow.captured_at, ResticSnapshotRow.snapshot_id)
    ):
        machine_progress = progress.get(machine_id)
        if machine_progress is None:
            # A newly registered machine can commit between these queries under READ COMMITTED.
            # The next poll will include it in the machine list.
            continue
        state = _snapshot_import_state(inventory_status, processing_status, codex_metadata_status)
        machine_progress.known_snapshot_count += 1
        if (
            inventory_status == "complete"
            and processing_status == "complete"
            and codex_metadata_status in ("complete", "partial")
        ):
            machine_progress.completed_snapshot_count += 1
        if state in (
            SnapshotImportState.INVENTORY_FAILED,
            SnapshotImportState.PROCESSING_FAILED,
            SnapshotImportState.CODEX_METADATA_FAILED,
        ):
            machine_progress.failed_snapshot_count += 1
        if codex_metadata_status == "pending":
            machine_progress.codex_metadata_pending_snapshot_count += 1
        elif codex_metadata_status == "failed":
            machine_progress.codex_metadata_failed_snapshot_count += 1
        elif codex_metadata_status == "partial":
            machine_progress.codex_metadata_partial_snapshot_count += 1
        snapshots_by_machine.setdefault(machine_id, []).append((captured_at, state))

    for machine_id, snapshots in snapshots_by_machine.items():
        unfinished = [row for row in snapshots if row[1] is not SnapshotImportState.COMPLETE]
        if unfinished:
            progress[machine_id].oldest_unfinished_snapshot_at = unfinished[0][0]
            progress[machine_id].oldest_unfinished_snapshot_state = unfinished[0][1]

    processed_statuses = {"not_indexed", "unclassified", "deleted", "complete", "partial"}
    for machine_id, file_status, count in session.execute(
        select(SnapshotFileRow.machine_id, SnapshotFileRow.status, func.count())
        .group_by(SnapshotFileRow.machine_id, SnapshotFileRow.status)
        .order_by(SnapshotFileRow.machine_id, SnapshotFileRow.status)
    ):
        machine_progress = progress.get(machine_id)
        if machine_progress is None:
            continue
        if file_status in processed_statuses:
            machine_progress.processed_file_count += count
        elif file_status == "pending":
            machine_progress.pending_file_count += count
        elif file_status == "failed":
            machine_progress.failed_file_count += count

    return [progress[machine.id] for machine in machines]


def _snapshot_import_state(
    inventory_status: str, processing_status: str, codex_metadata_status: str
) -> SnapshotImportState:
    if inventory_status == "failed":
        return SnapshotImportState.INVENTORY_FAILED
    if processing_status == "failed":
        return SnapshotImportState.PROCESSING_FAILED
    if codex_metadata_status == "failed":
        return SnapshotImportState.CODEX_METADATA_FAILED
    if inventory_status != "complete":
        return SnapshotImportState.INVENTORY_PENDING
    if codex_metadata_status == "pending":
        return SnapshotImportState.CODEX_METADATA_PENDING
    if processing_status != "complete":
        return SnapshotImportState.PROCESSING_PENDING
    return SnapshotImportState.COMPLETE


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
    latest_version_record_count = (
        select(FileVersionRow.record_count)
        .join(SnapshotFileRow, SnapshotFileRow.file_version_id == FileVersionRow.id)
        .join(
            ResticSnapshotRow,
            (ResticSnapshotRow.machine_id == SnapshotFileRow.machine_id)
            & (ResticSnapshotRow.snapshot_id == SnapshotFileRow.snapshot_id),
        )
        .where(FileVersionRow.session_id == SessionRow.id)
        .where(SnapshotFileRow.status.in_(("complete", "partial")))
        .order_by(ResticSnapshotRow.captured_at.desc(), ResticSnapshotRow.snapshot_id.desc())
        .limit(1)
        .scalar_subquery()
    )
    has_pending_file = exists(
        select(1).where(SnapshotFileRow.session_id == SessionRow.id, SnapshotFileRow.status == "pending")
    )
    has_failed_file = exists(
        select(1).where(SnapshotFileRow.session_id == SessionRow.id, SnapshotFileRow.status == "failed")
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
    events = []
    for _, record, payload in page_rows:
        parsed = parse_record_json(payload.raw_bytes)
        events.append(
            Event(
                ordinal=record.ordinal,
                byte_offset=record.byte_offset,
                timestamp=payload.timestamp,
                kind=payload.kind,
                parsed=parsed.parsed,
                parse_error=parsed.parse_error,
                raw=payload.raw_bytes.decode("utf-8", errors="replace").rstrip("\r\n"),
                raw_base64=base64.b64encode(payload.raw_bytes).decode("ascii"),
            )
        )
    next_cursor = events[-1].ordinal if has_more and events else None
    return EventPage(data=events, next_cursor=next_cursor)
