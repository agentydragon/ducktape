"""Read the snapshot and file import ledger."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from ai_session_archive.database import MachineRow, ResticSnapshotRow, SnapshotFileRow
from ai_session_archive.models import Machine, MachineImportProgress, SnapshotImportState
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
