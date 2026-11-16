"""Restic snapshot discovery and file inventory ledger."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session as DbSession

from ai_session_archive.database import ResticSnapshotRow, SessionRow, SnapshotFileRow
from ai_session_archive.models import Harness
from ai_session_archive.restic import ResticChange, ResticFile, ResticRepository, ResticSnapshot
from ai_session_archive.statuses import (
    CodexMetadataStatus,
    SnapshotCaptureStatus,
    SnapshotFileStatus,
    SnapshotInventoryStatus,
    SnapshotProcessingStatus,
)


@dataclass(frozen=True)
class HarnessRoot:
    """A configured harness root as it appears inside Restic snapshots."""

    harness: Harness
    path: PurePosixPath

    def __init__(self, harness: Harness, path: str | PurePosixPath):
        normalized = PurePosixPath(path)
        if str(normalized) in ("", "."):
            raise ValueError("harness root must be an explicit non-empty path")
        object.__setattr__(self, "harness", harness)
        object.__setattr__(self, "path", normalized)


@dataclass(frozen=True)
class ArchiveLocation:
    harness: Harness
    root: HarnessRoot
    relative_path: PurePosixPath
    source_path: str
    native_session_id: str
    parent_source_path: str | None


SnapshotInventoryEntry = tuple[str, str, str, int, datetime | None, str, str | None, ArchiveLocation | None]


def assert_snapshot_identity(row: ResticSnapshotRow, snapshot: ResticSnapshot) -> None:
    if (
        ResticSnapshot(
            id=row.snapshot_id,
            captured_at=row.captured_at,
            parent_id=row.parent_snapshot_id,
            tree_id=row.tree_id,
            paths=tuple(row.paths),
        )
        != snapshot
    ):
        raise RuntimeError(f"Restic metadata changed for snapshot {snapshot.id}")


def error_text(exc: Exception) -> str:
    message = str(exc).strip() or type(exc).__name__
    return f"{type(exc).__name__}: {message}"[:4000]


class SnapshotInventory:
    """Discovers snapshots and commits their full or diff-based file inventory."""

    def __init__(
        self,
        session_factory: Callable[[], DbSession],
        machine_id: str,
        restic: ResticRepository,
        roots: tuple[HarnessRoot, ...],
        ensure_session: Callable[[DbSession, ArchiveLocation], SessionRow],
    ) -> None:
        self._session_factory = session_factory
        self._machine_id = machine_id
        self._restic = restic
        self.roots = tuple(sorted(roots, key=lambda root: len(root.path.parts), reverse=True))
        self._ensure_session = ensure_session

    def discover(self, snapshots: list[ResticSnapshot]) -> None:
        """Commit the complete discovery ledger before potentially long file extraction."""
        with self._session_factory() as db, db.begin():
            existing_ids = set(
                db.scalars(
                    select(ResticSnapshotRow.snapshot_id).where(ResticSnapshotRow.machine_id == self._machine_id)
                )
            )
            for snapshot in snapshots:
                if snapshot.id not in existing_ids:
                    db.execute(
                        pg_insert(ResticSnapshotRow)
                        .values(
                            machine_id=self._machine_id,
                            snapshot_id=snapshot.id,
                            captured_at=snapshot.captured_at,
                            parent_snapshot_id=snapshot.parent_id,
                            tree_id=snapshot.tree_id,
                            paths=list(snapshot.paths),
                            comparison_snapshot_id=None,
                            inventory_status=SnapshotInventoryStatus.PENDING,
                            processing_status=SnapshotProcessingStatus.PENDING,
                            codex_metadata_status=CodexMetadataStatus.PENDING,
                            capture_status=SnapshotCaptureStatus.UNKNOWN,
                        )
                        .on_conflict_do_nothing()
                    )
                    existing_ids.add(snapshot.id)

    def new_snapshot_row(self, snapshot: ResticSnapshot) -> ResticSnapshotRow:
        return ResticSnapshotRow(
            machine_id=self._machine_id,
            snapshot_id=snapshot.id,
            captured_at=snapshot.captured_at,
            parent_snapshot_id=snapshot.parent_id,
            tree_id=snapshot.tree_id,
            paths=list(snapshot.paths),
            comparison_snapshot_id=None,
            inventory_status=SnapshotInventoryStatus.PENDING,
            processing_status=SnapshotProcessingStatus.PENDING,
            codex_metadata_status=CodexMetadataStatus.PENDING,
            capture_status=SnapshotCaptureStatus.UNKNOWN,
        )

    def states(self, available_snapshot_ids: set[str]) -> dict[str, tuple[str, str, str]]:
        with self._session_factory() as db:
            return {
                snapshot_id: (inventory_status, processing_status, codex_metadata_status)
                for snapshot_id, inventory_status, processing_status, codex_metadata_status in db.execute(
                    select(
                        ResticSnapshotRow.snapshot_id,
                        ResticSnapshotRow.inventory_status,
                        ResticSnapshotRow.processing_status,
                        ResticSnapshotRow.codex_metadata_status,
                    ).where(
                        ResticSnapshotRow.machine_id == self._machine_id,
                        ResticSnapshotRow.snapshot_id.in_(available_snapshot_ids),
                    )
                )
            }

    def all_inventoried_and_processed(self, available_snapshot_ids: set[str]) -> bool:
        with self._session_factory() as db:
            return all(
                row.inventory_status == SnapshotInventoryStatus.COMPLETE
                and row.processing_status == SnapshotProcessingStatus.COMPLETE
                for row in db.scalars(
                    select(ResticSnapshotRow).where(
                        ResticSnapshotRow.machine_id == self._machine_id,
                        ResticSnapshotRow.snapshot_id.in_(available_snapshot_ids),
                    )
                )
            )

    def prepare(self, snapshot: ResticSnapshot, available_snapshot_ids: set[str]) -> None:
        try:
            while True:
                with self._session_factory() as db, db.begin():
                    row = db.scalar(
                        select(ResticSnapshotRow)
                        .where(
                            ResticSnapshotRow.machine_id == self._machine_id,
                            ResticSnapshotRow.snapshot_id == snapshot.id,
                        )
                        .with_for_update()
                    )
                    if row is None:
                        row = self.new_snapshot_row(snapshot)
                        db.add(row)
                        db.flush()
                    assert_snapshot_identity(row, snapshot)
                    if row.inventory_status == SnapshotInventoryStatus.COMPLETE:
                        return
                    comparison_snapshot_id = self._usable_parent(db, snapshot, available_snapshot_ids)

                if comparison_snapshot_id is None:
                    entries = self._initial_entries(self._restic.list_files(snapshot.id))
                else:
                    changes = self._restic.diff_snapshots(comparison_snapshot_id, snapshot.id)
                    entries = self.changed_entries(changes)

                with self._session_factory() as db, db.begin():
                    row = db.scalar(
                        select(ResticSnapshotRow)
                        .where(
                            ResticSnapshotRow.machine_id == self._machine_id,
                            ResticSnapshotRow.snapshot_id == snapshot.id,
                        )
                        .with_for_update()
                    )
                    if row is None:
                        raise RuntimeError(f"snapshot ledger row disappeared for {snapshot.id}")
                    assert_snapshot_identity(row, snapshot)
                    if row.inventory_status == SnapshotInventoryStatus.COMPLETE:
                        return
                    current_comparison_snapshot_id = self._usable_parent(
                        db, snapshot, available_snapshot_ids, lock=True
                    )
                    if comparison_snapshot_id is not None and current_comparison_snapshot_id != comparison_snapshot_id:
                        # A diff is valid only while its exact parent remains fully processed.
                        # Re-read the snapshot independently if that trust boundary changed.
                        continue

                    row.comparison_snapshot_id = comparison_snapshot_id
                    row.inventory_error = None
                    for path, modifier, file_type, size, modified_at, status, error, location in entries:
                        existing = db.get(SnapshotFileRow, (self._machine_id, snapshot.id, path))
                        if existing is not None:
                            continue
                        session_id = None
                        if location is not None:
                            session_row = self._ensure_session(db, location)
                            session_id = session_row.id
                        db.add(
                            SnapshotFileRow(
                                machine_id=self._machine_id,
                                snapshot_id=snapshot.id,
                                path=path,
                                session_id=session_id,
                                change_kind=modifier,
                                file_type=file_type,
                                byte_size=size,
                                modified_at=modified_at,
                                status=status,
                                error=error,
                            )
                        )
                    row.inventory_status = SnapshotInventoryStatus.COMPLETE
                    row.processing_status = SnapshotProcessingStatus.PENDING
                    return
        except Exception as exc:
            self.mark_failed(snapshot, exc)

    def _usable_parent(
        self, db: DbSession, snapshot: ResticSnapshot, available_snapshot_ids: set[str], *, lock: bool = False
    ) -> str | None:
        parent_id = snapshot.parent_id
        if parent_id is None or parent_id not in available_snapshot_ids:
            return None
        parent = (
            db.scalar(
                select(ResticSnapshotRow)
                .where(ResticSnapshotRow.machine_id == self._machine_id, ResticSnapshotRow.snapshot_id == parent_id)
                .with_for_update()
            )
            if lock
            else db.get(ResticSnapshotRow, (self._machine_id, parent_id))
        )
        if parent is None:
            # A pruned/missing diff base cannot safely seed path state. Inventory this snapshot itself.
            return None
        if (
            parent.inventory_status != SnapshotInventoryStatus.COMPLETE
            or parent.processing_status != SnapshotProcessingStatus.COMPLETE
        ):
            # Work is independent when listed directly; never inherit from an unfinished ledger position.
            return None
        return parent_id

    def _initial_entries(self, files: Iterable[ResticFile]) -> list[SnapshotInventoryEntry]:
        entries: list[SnapshotInventoryEntry] = []
        for file in files:
            if not file.path.endswith(".jsonl"):
                continue
            location = self.classify(file.path)
            if location is None:
                status, error = SnapshotFileStatus.UNCLASSIFIED, None
            elif file.file_type != "file":
                status, error = (
                    SnapshotFileStatus.NOT_INDEXED,
                    f"JSONL path is a {file.file_type}, not a regular file",
                )
            else:
                status, error = SnapshotFileStatus.PENDING, None
            entries.append(
                (file.path, "inventory", file.file_type, file.byte_size, file.modified_at, status, error, location)
            )
        return entries

    def changed_entries(self, changes: Iterable[ResticChange]) -> list[SnapshotInventoryEntry]:
        combined: dict[str, list[str]] = {}
        for change in changes:
            if change.path.endswith(".jsonl"):
                combined.setdefault(change.path, []).append(change.modifier)
        entries: list[SnapshotInventoryEntry] = []
        for path, modifiers in sorted(combined.items()):
            modifier = ",".join(dict.fromkeys(modifiers))
            location = self.classify(path)
            if location is None:
                status, error = SnapshotFileStatus.UNCLASSIFIED, None
            elif modifier == "-":
                status, error = SnapshotFileStatus.DELETED, None
            elif any(part not in KNOWN_DIFF_MODIFIERS for part in modifiers):
                status, error = SnapshotFileStatus.FAILED, f"unrecognized Restic diff modifier: {modifier}"
            else:
                status, error = SnapshotFileStatus.PENDING, None
            entries.append((path, modifier, "file", 0, None, status, error, location))
        return entries

    def classify(self, path: str) -> ArchiveLocation | None:
        archived_path = PurePosixPath(path)
        archived_parts = archived_path.parts
        for root in self.roots:
            root_parts = root.path.parts
            if archived_parts[: len(root_parts)] == root_parts:
                relative_parts = archived_parts[len(root_parts) :]
            elif tuple(part for part in archived_parts if part != "/")[: len(root_parts)] == tuple(
                part for part in root_parts if part != "/"
            ):
                stripped = tuple(part for part in archived_parts if part != "/")
                relative_parts = stripped[len(tuple(part for part in root_parts if part != "/")) :]
            else:
                continue
            relative = PurePosixPath(*relative_parts)
            if not relative_parts or relative.suffix != ".jsonl":
                return None
            if root.harness == Harness.CLAUDE:
                supported = len(relative_parts) >= 3 and relative_parts[0] == "projects"
            else:
                supported = len(relative_parts) >= 2 and relative_parts[0] in ("sessions", "archived_sessions")
            if not supported:
                return None
            parent_source_path = None
            if root.harness == Harness.CLAUDE and "subagents" in relative_parts:
                index = len(relative_parts) - 1 - tuple(reversed(relative_parts)).index("subagents")
                parent_parts = list(relative_parts[:index])
                if parent_parts:
                    parent_parts[-1] = f"{parent_parts[-1]}.jsonl"
                    parent_relative = PurePosixPath(*parent_parts)
                    archive_root_parts = archived_parts[: len(archived_parts) - len(relative_parts)]
                    parent_source_path = str(PurePosixPath(*archive_root_parts) / parent_relative)
            return ArchiveLocation(
                harness=root.harness,
                root=root,
                relative_path=relative,
                source_path=path,
                native_session_id=relative.stem,
                parent_source_path=parent_source_path,
            )
        return None

    def mark_failed(self, snapshot: ResticSnapshot, exc: Exception) -> None:
        with self._session_factory() as db, db.begin():
            row = db.scalar(
                select(ResticSnapshotRow)
                .where(ResticSnapshotRow.machine_id == self._machine_id, ResticSnapshotRow.snapshot_id == snapshot.id)
                .with_for_update()
            )
            if row is None:
                row = self.new_snapshot_row(snapshot)
                db.add(row)
            if row.inventory_status == SnapshotInventoryStatus.COMPLETE:
                return
            row.inventory_status = SnapshotInventoryStatus.FAILED
            row.inventory_error = error_text(exc)
            row.processing_status = SnapshotProcessingStatus.FAILED


KNOWN_DIFF_MODIFIERS = frozenset(("+", "-", "M", "T", "?", "U"))
