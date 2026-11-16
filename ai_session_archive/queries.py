"""Read-side queries shared by the archive importer and HTTP API."""

import base64
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import exists, false, func, or_, select
from sqlalchemy.orm import Session as DbSession

from ai_session_archive.database import FileVersionRow, MachineRow, ResticSnapshotRow, SessionRow, SnapshotFileRow
from ai_session_archive.models import (
    Harness,
    Machine,
    MachineImportProgress,
    SessionKind,
    SessionPage,
    SessionSummary,
    SnapshotImportState,
    TranscriptStatus,
)
from ai_session_archive.statuses import (
    CodexMetadataStatus,
    SnapshotFileStatus,
    SnapshotInventoryStatus,
    SnapshotProcessingStatus,
)


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
            inventory_status == SnapshotInventoryStatus.COMPLETE
            and processing_status == SnapshotProcessingStatus.COMPLETE
            and codex_metadata_status in (CodexMetadataStatus.COMPLETE, CodexMetadataStatus.PARTIAL)
        ):
            machine_progress.completed_snapshot_count += 1
        if state in (
            SnapshotImportState.INVENTORY_FAILED,
            SnapshotImportState.PROCESSING_FAILED,
            SnapshotImportState.CODEX_METADATA_FAILED,
        ):
            machine_progress.failed_snapshot_count += 1
        if codex_metadata_status == CodexMetadataStatus.PENDING:
            machine_progress.codex_metadata_pending_snapshot_count += 1
        elif codex_metadata_status == CodexMetadataStatus.FAILED:
            machine_progress.codex_metadata_failed_snapshot_count += 1
        elif codex_metadata_status == CodexMetadataStatus.PARTIAL:
            machine_progress.codex_metadata_partial_snapshot_count += 1
        snapshots_by_machine.setdefault(machine_id, []).append((captured_at, state))

    for machine_id, snapshots in snapshots_by_machine.items():
        unfinished = [row for row in snapshots if row[1] is not SnapshotImportState.COMPLETE]
        if unfinished:
            progress[machine_id].oldest_unfinished_snapshot_at = unfinished[0][0]
            progress[machine_id].oldest_unfinished_snapshot_state = unfinished[0][1]

    processed_statuses = {
        SnapshotFileStatus.NOT_INDEXED,
        SnapshotFileStatus.UNCLASSIFIED,
        SnapshotFileStatus.DELETED,
        SnapshotFileStatus.COMPLETE,
        SnapshotFileStatus.PARTIAL,
    }
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
        elif file_status == SnapshotFileStatus.PENDING:
            machine_progress.pending_file_count += count
        elif file_status == SnapshotFileStatus.FAILED:
            machine_progress.failed_file_count += count

    return [progress[machine.id] for machine in machines]


def _snapshot_import_state(
    inventory_status: str, processing_status: str, codex_metadata_status: str
) -> SnapshotImportState:
    if inventory_status == SnapshotInventoryStatus.FAILED:
        return SnapshotImportState.INVENTORY_FAILED
    if processing_status == SnapshotProcessingStatus.FAILED:
        return SnapshotImportState.PROCESSING_FAILED
    if codex_metadata_status == CodexMetadataStatus.FAILED:
        return SnapshotImportState.CODEX_METADATA_FAILED
    if inventory_status != SnapshotInventoryStatus.COMPLETE:
        return SnapshotImportState.INVENTORY_PENDING
    if codex_metadata_status == CodexMetadataStatus.PENDING:
        return SnapshotImportState.CODEX_METADATA_PENDING
    if processing_status != SnapshotProcessingStatus.COMPLETE:
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
