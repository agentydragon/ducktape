"""Idempotent Restic snapshot ingestion for local Claude and Codex JSONL."""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, BinaryIO, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select, text
from sqlalchemy.orm import Session as DbSession, load_only

from x.ai_session_archive.codex_metadata import read_codex_titles
from x.ai_session_archive.database import (
    CodexTitleObservationRow,
    FileVersionRecordRow,
    FileVersionRow,
    MachineRow,
    RecordPayloadRow,
    ResticSnapshotRow,
    SessionRootRow,
    SessionRow,
    SnapshotFileRow,
)
from x.ai_session_archive.models import Harness
from x.ai_session_archive.restic import ResticChange, ResticFile, ResticRepository, ResticSnapshot

PARSER_VERSION = "jsonl-v1"
CHECKPOINT_INTERVAL = 64
RECORD_BATCH_MAX_BYTES = 4 * 1024 * 1024
_KNOWN_DIFF_MODIFIERS = frozenset(("+", "-", "M", "T", "?", "U"))


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
class ArchiveSource:
    machine_id: str
    display_name: str
    restic: ResticRepository
    roots: tuple[HarnessRoot, ...]

    def __post_init__(self) -> None:
        if not self.machine_id or len(self.machine_id) > 100:
            raise ValueError("machine_id must contain 1 to 100 characters")
        if not self.display_name or len(self.display_name) > 200:
            raise ValueError("display_name must contain 1 to 200 characters")
        if not self.roots:
            raise ValueError("at least one configured Claude or Codex root is required")


@dataclass(frozen=True)
class IngestReport:
    snapshots_seen: int
    snapshots_complete: int
    snapshots_incomplete: int
    files_complete: int
    files_partial: int
    files_failed: int
    files_unclassified: int
    codex_metadata_partial: int


@dataclass(frozen=True)
class _Location:
    harness: Harness
    root: HarnessRoot
    relative_path: PurePosixPath
    source_path: str
    native_session_id: str
    parent_source_path: str | None


@dataclass(frozen=True)
class _ParsedRecord:
    raw_bytes: bytes
    byte_offset: int
    sha256: str
    parsed: Any | None
    parse_error: str | None
    timestamp: datetime | None
    kind: str | None


class SnapshotIngestor:
    """Imports all retained snapshots for one Restic repository into Postgres."""

    def __init__(self, session_factory: Callable[[], DbSession], source: ArchiveSource):
        self._session_factory = session_factory
        self._source = source
        self._roots = tuple(sorted(source.roots, key=lambda root: len(root.path.parts), reverse=True))

    def ingest(self) -> IngestReport:
        snapshots = sorted(self._source.restic.list_snapshots(), key=lambda row: (row.captured_at, row.id))
        self._ensure_source()
        self._discover_snapshots(snapshots)
        available_snapshot_ids = {snapshot.id for snapshot in snapshots}
        if snapshots:
            # Make the latest names visible before a potentially long historical transcript import.
            self._process_codex_metadata(snapshots[-1])
            self._project_codex_titles(allow_index_only=False)
        for snapshot in snapshots:
            self._prepare_snapshot(snapshot, available_snapshot_ids)
            self._process_codex_metadata(snapshot)
            self._process_snapshot(snapshot)
        with self._session_factory() as db:
            all_inventoried = all(
                row.inventory_status == "complete" and row.processing_status == "complete"
                for row in db.scalars(
                    select(ResticSnapshotRow).where(
                        ResticSnapshotRow.machine_id == self._source.machine_id,
                        ResticSnapshotRow.snapshot_id.in_(available_snapshot_ids),
                    )
                )
            )
        self._project_codex_titles(allow_index_only=all_inventoried)
        return self._report(available_snapshot_ids)

    def _discover_snapshots(self, snapshots: list[ResticSnapshot]) -> None:
        """Commit the complete discovery ledger before potentially long file extraction."""
        with self._session_factory() as db, db.begin():
            existing_ids = set(
                db.scalars(
                    select(ResticSnapshotRow.snapshot_id).where(ResticSnapshotRow.machine_id == self._source.machine_id)
                )
            )
            for snapshot in snapshots:
                if snapshot.id not in existing_ids:
                    db.add(self._new_snapshot_row(snapshot))
                    existing_ids.add(snapshot.id)

    def _new_snapshot_row(self, snapshot: ResticSnapshot) -> ResticSnapshotRow:
        return ResticSnapshotRow(
            machine_id=self._source.machine_id,
            snapshot_id=snapshot.id,
            captured_at=snapshot.captured_at,
            parent_snapshot_id=snapshot.parent_id,
            tree_id=snapshot.tree_id,
            paths=list(snapshot.paths),
            comparison_snapshot_id=None,
            inventory_status="pending",
            processing_status="pending",
            codex_metadata_status="pending",
            capture_status="unknown",
        )

    def _ensure_source(self) -> None:
        with self._session_factory() as db, db.begin():
            machine = db.get(MachineRow, self._source.machine_id)
            if machine is None:
                db.add(MachineRow(id=self._source.machine_id, display_name=self._source.display_name))
            else:
                machine.display_name = self._source.display_name
            configured = {
                (row.harness, row.root_path)
                for row in db.scalars(
                    select(SessionRootRow).where(SessionRootRow.machine_id == self._source.machine_id)
                )
            }
            for root in self._roots:
                key = (root.harness.value, str(root.path))
                if key not in configured:
                    db.add(SessionRootRow(machine_id=self._source.machine_id, harness=key[0], root_path=key[1]))

    def _prepare_snapshot(self, snapshot: ResticSnapshot, available_snapshot_ids: set[str]) -> None:
        try:
            with self._session_factory() as db, db.begin():
                row = db.scalar(
                    select(ResticSnapshotRow)
                    .where(
                        ResticSnapshotRow.machine_id == self._source.machine_id,
                        ResticSnapshotRow.snapshot_id == snapshot.id,
                    )
                    .with_for_update()
                )
                if row is None:
                    row = self._new_snapshot_row(snapshot)
                    db.add(row)
                    db.flush()
                if row.inventory_status == "complete":
                    return

                comparison_snapshot_id = self._usable_parent(db, snapshot, available_snapshot_ids)
                if comparison_snapshot_id is None:
                    files = self._source.restic.list_files(snapshot.id)
                    entries = self._initial_entries(files)
                else:
                    changes = self._source.restic.diff_snapshots(comparison_snapshot_id, snapshot.id)
                    entries = self._changed_entries(changes)

                row.comparison_snapshot_id = comparison_snapshot_id
                row.inventory_error = None
                for path, modifier, file_type, size, modified_at, status, error, location in entries:
                    existing = db.get(SnapshotFileRow, (self._source.machine_id, snapshot.id, path))
                    if existing is not None:
                        continue
                    session_id = None
                    if location is not None:
                        session_row = self._ensure_session(db, location)
                        session_id = session_row.id
                    db.add(
                        SnapshotFileRow(
                            machine_id=self._source.machine_id,
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
                row.inventory_status = "complete"
                row.processing_status = "pending"
        except Exception as exc:
            self._mark_inventory_failed(snapshot, exc)

    def _usable_parent(self, db: DbSession, snapshot: ResticSnapshot, available_snapshot_ids: set[str]) -> str | None:
        parent_id = snapshot.parent_id
        if parent_id is None or parent_id not in available_snapshot_ids:
            return None
        parent = db.get(ResticSnapshotRow, (self._source.machine_id, parent_id))
        if parent is None:
            # A pruned/missing diff base cannot safely seed path state. Inventory this snapshot itself.
            return None
        if parent.inventory_status != "complete" or parent.processing_status != "complete":
            # Work is independent when listed directly; never inherit from an unfinished ledger position.
            return None
        return parent_id

    def _initial_entries(
        self, files: Iterable[ResticFile]
    ) -> list[tuple[str, str, str, int, datetime | None, str, str | None, _Location | None]]:
        entries: list[tuple[str, str, str, int, datetime | None, str, str | None, _Location | None]] = []
        for file in files:
            if not file.path.endswith(".jsonl"):
                continue
            location = self._classify(file.path)
            if location is None:
                status, error = "unclassified", None
            elif file.file_type != "file":
                status, error = "not_indexed", f"JSONL path is a {file.file_type}, not a regular file"
            else:
                status, error = "pending", None
            entries.append(
                (file.path, "inventory", file.file_type, file.byte_size, file.modified_at, status, error, location)
            )
        return entries

    def _changed_entries(
        self, changes: Iterable[ResticChange]
    ) -> list[tuple[str, str, str, int, datetime | None, str, str | None, _Location | None]]:
        combined: dict[str, list[str]] = {}
        for change in changes:
            if change.path.endswith(".jsonl"):
                combined.setdefault(change.path, []).append(change.modifier)
        entries: list[tuple[str, str, str, int, datetime | None, str, str | None, _Location | None]] = []
        for path, modifiers in sorted(combined.items()):
            modifier = ",".join(dict.fromkeys(modifiers))
            location = self._classify(path)
            if location is None:
                status, error = "unclassified", None
            elif modifier == "-":
                status, error = "deleted", None
            elif any(part not in _KNOWN_DIFF_MODIFIERS for part in modifiers):
                status, error = "failed", f"unrecognized Restic diff modifier: {modifier}"
            else:
                status, error = "pending", None
            entries.append((path, modifier, "file", 0, None, status, error, location))
        return entries

    def _classify(self, path: str) -> _Location | None:
        archived_path = PurePosixPath(path)
        archived_parts = archived_path.parts
        for root in self._roots:
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
            return _Location(
                harness=root.harness,
                root=root,
                relative_path=relative,
                source_path=path,
                native_session_id=relative.stem,
                parent_source_path=parent_source_path,
            )
        return None

    def _ensure_session(self, db: DbSession, location: _Location, inferred: bool = False) -> SessionRow:
        session_id = _session_id(self._source.machine_id, location.harness, location.source_path)
        row = db.get(SessionRow, session_id)
        if row is not None:
            if not inferred and row.identity_inferred:
                row.identity_inferred = False
                row.source_relative_path = location.relative_path.as_posix()
                if row.native_session_id_inferred:
                    row.native_session_id = location.native_session_id
                if location.parent_source_path is not None:
                    row.parent_session_id = _session_id(
                        self._source.machine_id, location.harness, location.parent_source_path
                    )
                    row.parent_link_kind = "claude_path_inferred"
            return row
        parent_id = None
        parent_link_kind = None
        if not inferred and location.parent_source_path is not None:
            parent_id = _session_id(self._source.machine_id, location.harness, location.parent_source_path)
            parent_link_kind = "claude_path_inferred"
            parent_location = self._classify(location.parent_source_path)
            if parent_location is not None:
                self._ensure_session(db, parent_location, inferred=True)
        row = SessionRow(
            id=session_id,
            machine_id=self._source.machine_id,
            harness=location.harness.value,
            source_path=location.source_path,
            source_relative_path=location.relative_path.as_posix(),
            native_session_id=location.native_session_id,
            native_session_id_inferred=True,
            parent_session_id=parent_id,
            parent_link_kind=parent_link_kind,
            identity_inferred=inferred,
        )
        db.add(row)
        db.flush()
        return row

    def _process_snapshot(self, snapshot: ResticSnapshot) -> None:
        with self._session_factory() as db:
            files = list(
                db.scalars(
                    select(SnapshotFileRow)
                    .where(
                        SnapshotFileRow.machine_id == self._source.machine_id,
                        SnapshotFileRow.snapshot_id == snapshot.id,
                    )
                    .order_by(SnapshotFileRow.path)
                )
            )
        for file_row in files:
            if file_row.status not in ("pending", "failed"):
                continue
            self._process_file(snapshot, file_row.path)
        with self._session_factory() as db, db.begin():
            snapshot_row = db.scalar(
                select(ResticSnapshotRow)
                .where(
                    ResticSnapshotRow.machine_id == self._source.machine_id,
                    ResticSnapshotRow.snapshot_id == snapshot.id,
                )
                .with_for_update()
            )
            if snapshot_row is None:
                return
            if snapshot_row.inventory_status != "complete":
                snapshot_row.processing_status = "failed"
                return
            statuses = list(
                db.scalars(
                    select(SnapshotFileRow.status).where(
                        SnapshotFileRow.machine_id == self._source.machine_id,
                        SnapshotFileRow.snapshot_id == snapshot.id,
                    )
                )
            )
            failed = any(status in ("pending", "failed") for status in statuses)
            snapshot_row.processing_status = "failed" if failed else "complete"

    def _process_codex_metadata(self, snapshot: ResticSnapshot) -> None:
        with self._session_factory() as db, db.begin():
            row = db.get(ResticSnapshotRow, (self._source.machine_id, snapshot.id))
            if row is None:
                row = self._new_snapshot_row(snapshot)
                db.add(row)
            if row.codex_metadata_status in ("complete", "partial"):
                return
        roots = tuple(root.path for root in self._roots if root.harness == Harness.CODEX)
        try:
            observations, warnings, retry_errors = read_codex_titles(self._source.restic, snapshot.id, roots)
            with self._session_factory() as db, db.begin():
                row = db.get(ResticSnapshotRow, (self._source.machine_id, snapshot.id), with_for_update=True)
                if row is None or row.codex_metadata_status in ("complete", "partial"):
                    return
                existing_observations = {
                    observation.native_session_id: observation
                    for observation in db.scalars(
                        select(CodexTitleObservationRow).where(
                            CodexTitleObservationRow.machine_id == self._source.machine_id,
                            CodexTitleObservationRow.snapshot_id == snapshot.id,
                        )
                    )
                }
                for item in observations:
                    existing = existing_observations.get(item.native_session_id)
                    if existing is None:
                        db.add(
                            CodexTitleObservationRow(
                                machine_id=self._source.machine_id,
                                snapshot_id=snapshot.id,
                                native_session_id=item.native_session_id,
                                source_path=item.source_path,
                                title=item.title,
                                title_source=item.title_source,
                                updated_at=item.updated_at,
                            )
                        )
                    else:
                        existing.source_path = item.source_path
                        existing.title = item.title
                        existing.title_source = item.title_source
                        existing.updated_at = item.updated_at
                row.codex_metadata_status = "failed" if retry_errors else "partial" if warnings else "complete"
                issues = [*retry_errors, *warnings]
                row.codex_metadata_error = "; ".join(issues)[:4000] if issues else None
        except Exception as exc:
            with self._session_factory() as db, db.begin():
                row = db.get(ResticSnapshotRow, (self._source.machine_id, snapshot.id), with_for_update=True)
                if row is not None:
                    row.codex_metadata_status = "failed"
                    row.codex_metadata_error = _error_text(exc)

    def _project_codex_titles(self, *, allow_index_only: bool) -> None:
        with self._session_factory() as db, db.begin():
            sessions = list(
                db.scalars(
                    select(SessionRow).where(
                        SessionRow.machine_id == self._source.machine_id, SessionRow.harness == Harness.CODEX.value
                    )
                )
            )
            by_path = {row.source_path: row for row in sessions if not row.native_session_id_conflict}
            by_native: dict[str, list[SessionRow]] = {}
            for row in sessions:
                if row.native_session_id and not row.native_session_id_conflict and not row.native_session_id_inferred:
                    by_native.setdefault(row.native_session_id, []).append(row)
            latest: dict[UUID, tuple[datetime, str, str]] = {}
            observations = db.execute(
                select(CodexTitleObservationRow, ResticSnapshotRow.captured_at)
                .join(
                    ResticSnapshotRow,
                    (ResticSnapshotRow.machine_id == CodexTitleObservationRow.machine_id)
                    & (ResticSnapshotRow.snapshot_id == CodexTitleObservationRow.snapshot_id),
                )
                .where(CodexTitleObservationRow.machine_id == self._source.machine_id)
                # Matching below depends only on native ID and physical path.
                # Older observations of that pair cannot win the title rank.
                # Reduce them in PostgreSQL before SQLAlchemy buffers ORM rows:
                # the full history contains millions of observations per host.
                .distinct(CodexTitleObservationRow.native_session_id, CodexTitleObservationRow.source_path)
                .order_by(
                    CodexTitleObservationRow.native_session_id,
                    CodexTitleObservationRow.source_path,
                    ResticSnapshotRow.captured_at.desc(),
                    CodexTitleObservationRow.snapshot_id.desc(),
                    CodexTitleObservationRow.title.desc(),
                )
            )
            for observation, captured_at in observations:
                match = by_path.get(observation.source_path) if observation.source_path else None
                if match is not None and match.native_session_id != observation.native_session_id:
                    continue
                if match is None and not observation.source_path and allow_index_only:
                    candidates = by_native.get(observation.native_session_id, [])
                    match = candidates[0] if len(candidates) == 1 else None
                if match is None:
                    continue
                rank = (captured_at, observation.snapshot_id, observation.title)
                if match.id not in latest or rank > latest[match.id]:
                    latest[match.id] = rank
            for row in sessions:
                if row.id in latest:
                    row.title = latest[row.id][2]
                    row.codex_title_from_metadata = True
                elif row.codex_title_from_metadata and allow_index_only:
                    row.title = None
                    row.codex_title_from_metadata = False

    def _process_file(self, snapshot: ResticSnapshot, path: str) -> None:
        try:
            with self._session_factory() as db, db.begin():
                row = db.scalar(
                    select(SnapshotFileRow)
                    .where(
                        SnapshotFileRow.machine_id == self._source.machine_id,
                        SnapshotFileRow.snapshot_id == snapshot.id,
                        SnapshotFileRow.path == path,
                    )
                    .with_for_update()
                )
                if row is None or row.status not in ("pending", "failed"):
                    return
                location = self._classify(path)
                if location is None:
                    row.status = "unclassified"
                    return
                if row.change_kind == "-":
                    row.status = "deleted"
                    return
                if any(
                    part not in _KNOWN_DIFF_MODIFIERS and part != "inventory" for part in row.change_kind.split(",")
                ):
                    raise ValueError(f"unrecognized Restic diff modifier: {row.change_kind}")

                with cast(BinaryIO, tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")) as contents:
                    self._source.restic.dump_to(snapshot.id, path, contents)
                    contents.seek(0)
                    sha256, byte_size, record_count = _fingerprint_file(contents)
                    contents.seek(0)
                    session_row = (
                        db.get(SessionRow, row.session_id) if row.session_id else self._ensure_session(db, location)
                    )
                    if session_row is None:
                        raise RuntimeError(f"missing session row for {path}")
                    version_row, metadata = self._store_version(
                        db,
                        snapshot=snapshot,
                        snapshot_file=row,
                        location=location,
                        session_row=session_row,
                        sha256=sha256,
                        byte_size=byte_size,
                        record_count=record_count,
                        contents=contents,
                    )
                    row.file_version_id = version_row.id
                    row.byte_size = byte_size
                    row.file_type = "file"
                    row.status = "complete" if version_row.complete else "partial"
                    row.error = None
                    self._update_session_metadata(db, session_row, metadata)
        except Exception as exc:
            self._mark_file_failed(snapshot.id, path, exc)

    def _store_version(
        self,
        db: DbSession,
        *,
        snapshot: ResticSnapshot,
        snapshot_file: SnapshotFileRow,
        location: _Location,
        session_row: SessionRow,
        sha256: str,
        byte_size: int,
        record_count: int,
        contents: BinaryIO,
    ) -> tuple[FileVersionRow, dict[str, Any]]:
        existing = db.scalar(
            select(FileVersionRow).where(FileVersionRow.session_id == session_row.id, FileVersionRow.sha256 == sha256)
        )
        if existing is not None:
            if existing.byte_size != byte_size or existing.parser_version != PARSER_VERSION:
                raise RuntimeError("file content hash collides with an incompatible stored version")
            if (snapshot.captured_at, snapshot.id) < (existing.first_seen_at, existing.first_snapshot_id):
                existing.first_seen_at = snapshot.captured_at
                existing.first_snapshot_id = snapshot.id
            existing.last_seen_at = max(existing.last_seen_at, snapshot.captured_at)
            return existing, {}

        previous = self._previous_version(db, snapshot_file)
        previous_depth = previous.delta_depth if previous is not None else -1
        if previous is None or previous_depth + 1 >= CHECKPOINT_INTERVAL:
            parent_version_id = None
            delta_depth = 0
            is_checkpoint = True
        else:
            parent_version_id = previous.id
            delta_depth = previous_depth + 1
            is_checkpoint = False
        version_id = uuid5(session_row.id, f"{PARSER_VERSION}:{sha256}")
        row = FileVersionRow(
            id=version_id,
            session_id=session_row.id,
            sha256=sha256,
            byte_size=byte_size,
            source_path=location.source_path,
            first_seen_at=snapshot.captured_at,
            last_seen_at=snapshot.captured_at,
            first_snapshot_id=snapshot.id,
            complete=False,
            parse_issue_count=0,
            record_count=record_count,
            parent_version_id=parent_version_id,
            shared_prefix_record_count=0,
            is_checkpoint=is_checkpoint,
            delta_depth=delta_depth,
            parser_version=PARSER_VERSION,
        )
        db.add(row)
        db.flush()
        metadata: dict[str, Any] = {}
        issue_count = 0
        shared_prefix = 0
        ordinal = 0
        matching_prefix = not is_checkpoint
        parent_ranks = _version_lineage_ranks(db, previous) if matching_prefix and previous is not None else {}
        while batch := _read_record_batch(contents, metadata, limit=500):
            payloads = self._get_payloads(db, batch)
            parent_ids = (
                _effective_payload_ids(db, previous, parent_ranks, ordinal, ordinal + len(batch))
                if matching_prefix and previous is not None
                else []
            )
            additions = []
            for index, (record, payload) in enumerate(zip(batch, payloads, strict=True)):
                if record.parse_error is not None:
                    issue_count += 1
                if matching_prefix:
                    parent_index = index
                    if parent_index < len(parent_ids) and parent_ids[parent_index] == payload.id:
                        shared_prefix += 1
                        continue
                    matching_prefix = False
                additions.append(
                    FileVersionRecordRow(
                        file_version_id=version_id,
                        ordinal=ordinal + index,
                        byte_offset=record.byte_offset,
                        payload_id=payload.id,
                    )
                )
            if additions:
                db.add_all(additions)
            ordinal += len(batch)
        if ordinal != record_count:
            raise RuntimeError(f"file changed while parsing: expected {record_count} JSONL records, read {ordinal}")
        row.shared_prefix_record_count = 0 if is_checkpoint else shared_prefix
        row.parse_issue_count = issue_count
        row.complete = issue_count == 0
        return row, metadata

    def _get_payloads(self, db: DbSession, records: list[_ParsedRecord]) -> list[RecordPayloadRow]:
        hashes = list(dict.fromkeys(record.sha256 for record in records))
        payload_by_hash: dict[str, RecordPayloadRow] = {}
        for start in range(0, len(hashes), 1000):
            batch = hashes[start : start + 1000]
            rows = db.scalars(
                select(RecordPayloadRow)
                .options(load_only(RecordPayloadRow.sha256, RecordPayloadRow.raw_bytes))
                .where(RecordPayloadRow.sha256.in_(batch), RecordPayloadRow.parser_version == PARSER_VERSION)
            )
            payload_by_hash.update((row.sha256, row) for row in rows)
        for record in records:
            if record.sha256 in payload_by_hash:
                payload = payload_by_hash[record.sha256]
                if payload.raw_bytes != record.raw_bytes:
                    raise RuntimeError("record SHA-256 collision detected")
                continue
            payload = RecordPayloadRow(
                id=uuid5(NAMESPACE_URL, f"ai-session-record:{PARSER_VERSION}:{record.sha256}"),
                sha256=record.sha256,
                parser_version=PARSER_VERSION,
                raw_bytes=record.raw_bytes,
                parsed=record.parsed,
                parse_error=record.parse_error,
                timestamp=record.timestamp,
                kind=record.kind,
            )
            db.add(payload)
            payload_by_hash[record.sha256] = payload
        db.flush()
        return [payload_by_hash[record.sha256] for record in records]

    def _previous_version(self, db: DbSession, current: SnapshotFileRow) -> FileVersionRow | None:
        # A path absent from many consecutive diffs must not cost one database round trip per snapshot.
        # Stop at the first independent inventory, matching the diff ancestry's trust boundary.
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
                    JOIN restic_snapshots AS parent
                      ON parent.machine_id = :machine_id AND parent.snapshot_id = ancestors.parent_snapshot_id
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
        if prior is None or prior.status not in ("complete", "partial") or prior.file_version_id is None:
            return None
        return db.get(FileVersionRow, prior.file_version_id)

    def _update_session_metadata(self, db: DbSession, row: SessionRow, metadata: Mapping[str, Any]) -> None:
        native_session_id = metadata.get("native_session_id")
        if row.harness == Harness.CODEX.value:
            candidates = set(row.native_session_id_candidates)
            if row.native_session_id and not row.native_session_id_inferred:
                candidates.add(row.native_session_id)
            metadata_candidates = metadata.get("native_session_id_candidates", [])
            if isinstance(metadata_candidates, list):
                candidates.update(item for item in metadata_candidates if isinstance(item, str) and item)
            if isinstance(native_session_id, str) and native_session_id:
                candidates.add(native_session_id)
            if len(candidates) > 1:
                row.native_session_id_candidates = sorted(candidates)
                row.native_session_id = None
                row.native_session_id_inferred = False
                row.native_session_id_conflict = True
                for candidate in candidates:
                    self._resolve_codex_children(db, row.machine_id, candidate)
            elif candidates and not row.native_session_id_conflict:
                row.native_session_id = next(iter(candidates))
                row.native_session_id_candidates = list(candidates)
                row.native_session_id_inferred = False
                self._resolve_codex_children(db, row.machine_id, row.native_session_id)
        if row.harness == Harness.CODEX.value:
            parent_candidates = set(row.parent_native_session_id_candidates)
            if row.parent_native_session_id:
                parent_candidates.add(row.parent_native_session_id)
            metadata_parent_candidates = metadata.get("parent_native_session_id_candidates", [])
            if isinstance(metadata_parent_candidates, list):
                parent_candidates.update(item for item in metadata_parent_candidates if isinstance(item, str) and item)
            parent_native_session_id = metadata.get("parent_native_session_id")
            if isinstance(parent_native_session_id, str) and parent_native_session_id:
                parent_candidates.add(parent_native_session_id)
            if len(parent_candidates) > 1:
                row.parent_native_session_id_candidates = sorted(parent_candidates)
                row.parent_native_session_id = None
                row.parent_native_session_id_conflict = True
                row.parent_session_id = None
                row.parent_link_kind = "codex_parent_thread_id_inconsistent"
            elif parent_candidates and not row.parent_native_session_id_conflict:
                row.parent_native_session_id = next(iter(parent_candidates))
                row.parent_native_session_id_candidates = list(parent_candidates)
                self._resolve_codex_parent_link(db, row)
        if row.cwd is None and metadata.get("cwd"):
            row.cwd = str(metadata["cwd"])
        if row.title is None and metadata.get("title"):
            row.title = str(metadata["title"])[:500]
        if row.harness == Harness.CODEX.value:
            self._apply_latest_codex_title(db, row)
        first = metadata.get("first_timestamp")
        last = metadata.get("last_timestamp")
        if isinstance(first, datetime) and isinstance(last, datetime):
            row.started_at = first if row.started_at is None else min(row.started_at, first)
            row.last_activity_at = last if row.last_activity_at is None else max(row.last_activity_at, last)

    def _apply_latest_codex_title(self, db: DbSession, row: SessionRow) -> None:
        if row.native_session_id_conflict or not row.native_session_id:
            return
        observations = db.execute(
            select(CodexTitleObservationRow, ResticSnapshotRow.captured_at)
            .join(
                ResticSnapshotRow,
                (ResticSnapshotRow.machine_id == CodexTitleObservationRow.machine_id)
                & (ResticSnapshotRow.snapshot_id == CodexTitleObservationRow.snapshot_id),
            )
            .where(
                CodexTitleObservationRow.machine_id == row.machine_id,
                CodexTitleObservationRow.native_session_id == row.native_session_id,
            )
            .order_by(ResticSnapshotRow.captured_at.desc(), CodexTitleObservationRow.snapshot_id.desc())
        )
        for observation, _ in observations:
            if observation.source_path == row.source_path:
                row.title = observation.title
                row.codex_title_from_metadata = True
                return

    def _resolve_codex_parent_link(self, db: DbSession, child: SessionRow) -> None:
        if child.parent_native_session_id_conflict:
            child.parent_session_id = None
            child.parent_link_kind = "codex_parent_thread_id_inconsistent"
            return
        parent_native_session_id = child.parent_native_session_id
        if not parent_native_session_id:
            return
        conflicting_parents = db.scalars(
            select(SessionRow).where(
                SessionRow.machine_id == child.machine_id,
                SessionRow.harness == Harness.CODEX.value,
                SessionRow.native_session_id_conflict.is_(True),
            )
        )
        if any(parent_native_session_id in parent.native_session_id_candidates for parent in conflicting_parents):
            child.parent_session_id = None
            child.parent_link_kind = "codex_parent_thread_id_inconsistent"
            return
        parents = list(
            db.scalars(
                select(SessionRow).where(
                    SessionRow.machine_id == child.machine_id,
                    SessionRow.harness == Harness.CODEX.value,
                    SessionRow.native_session_id == parent_native_session_id,
                    SessionRow.native_session_id_inferred.is_(False),
                    SessionRow.identity_inferred.is_(False),
                    SessionRow.id != child.id,
                )
            )
        )
        if len(parents) == 1:
            child.parent_session_id = parents[0].id
            child.parent_link_kind = "codex_parent_thread_id"
        else:
            child.parent_session_id = None
            suffix = "unresolved" if not parents else "ambiguous"
            child.parent_link_kind = f"codex_parent_thread_id_{suffix}"

    def _resolve_codex_children(self, db: DbSession, machine_id: str, native_session_id: str) -> None:
        children = db.scalars(
            select(SessionRow).where(
                SessionRow.machine_id == machine_id,
                SessionRow.harness == Harness.CODEX.value,
                SessionRow.parent_native_session_id == native_session_id,
            )
        )
        for child in children:
            self._resolve_codex_parent_link(db, child)

    def _mark_inventory_failed(self, snapshot: ResticSnapshot, exc: Exception) -> None:
        with self._session_factory() as db, db.begin():
            row = db.get(ResticSnapshotRow, (self._source.machine_id, snapshot.id))
            if row is None:
                row = self._new_snapshot_row(snapshot)
                db.add(row)
            row.inventory_status = "failed"
            row.inventory_error = _error_text(exc)
            row.processing_status = "failed"

    def _mark_file_failed(self, snapshot_id: str, path: str, exc: Exception) -> None:
        with self._session_factory() as db, db.begin():
            row = db.get(SnapshotFileRow, (self._source.machine_id, snapshot_id, path))
            if row is not None:
                row.status = "failed"
                row.error = _error_text(exc)

    def _report(self, available_snapshot_ids: set[str]) -> IngestReport:
        with self._session_factory() as db:
            snapshots = list(
                db.scalars(
                    select(ResticSnapshotRow).where(
                        ResticSnapshotRow.machine_id == self._source.machine_id,
                        ResticSnapshotRow.snapshot_id.in_(available_snapshot_ids),
                    )
                )
            )
            files = list(
                db.scalars(
                    select(SnapshotFileRow).where(
                        SnapshotFileRow.machine_id == self._source.machine_id,
                        SnapshotFileRow.snapshot_id.in_(available_snapshot_ids),
                    )
                )
            )
        return IngestReport(
            snapshots_seen=len(available_snapshot_ids),
            snapshots_complete=sum(
                row.inventory_status == "complete"
                and row.processing_status == "complete"
                and row.codex_metadata_status in ("complete", "partial")
                for row in snapshots
            ),
            snapshots_incomplete=sum(
                row.inventory_status != "complete"
                or row.processing_status != "complete"
                or row.codex_metadata_status not in ("complete", "partial")
                for row in snapshots
            ),
            files_complete=sum(row.status == "complete" for row in files),
            files_partial=sum(row.status == "partial" for row in files),
            files_failed=sum(row.status == "failed" for row in files),
            files_unclassified=sum(row.status == "unclassified" for row in files),
            codex_metadata_partial=sum(row.codex_metadata_status == "partial" for row in snapshots),
        )


def _session_id(machine_id: str, harness: Harness, source_path: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"ai-session:{machine_id}:{harness.value}:{source_path}")


def _error_text(exc: Exception) -> str:
    message = str(exc).strip() or type(exc).__name__
    return f"{type(exc).__name__}: {message}"[:4000]


def _fingerprint_file(source: BinaryIO) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    byte_size = 0
    newline_count = 0
    final_byte = b""
    while chunk := source.read(1024 * 1024):
        digest.update(chunk)
        byte_size += len(chunk)
        newline_count += chunk.count(b"\n")
        final_byte = chunk[-1:]
    record_count = newline_count + (1 if byte_size and final_byte != b"\n" else 0)
    return digest.hexdigest(), byte_size, record_count


def _read_record_batch(source: BinaryIO, metadata: dict[str, Any], *, limit: int) -> list[_ParsedRecord]:
    records: list[_ParsedRecord] = []
    batch_bytes = 0
    byte_offset = int(metadata.get("_byte_offset", 0))
    while len(records) < limit and batch_bytes < RECORD_BATCH_MAX_BYTES and (raw_bytes := source.readline()):
        content = raw_bytes[:-1] if raw_bytes.endswith(b"\n") else raw_bytes
        if content.endswith(b"\r"):
            content = content[:-1]
        parsed = None
        parse_error = None
        timestamp = None
        kind = None
        try:
            text = content.decode("utf-8", errors="strict")
            parsed = json.loads(text, parse_constant=_reject_json_constant)
            _validate_jsonb(parsed)
            if isinstance(parsed, dict):
                timestamp = _record_timestamp(parsed)
                kind = _record_kind(parsed)
                _extract_session_metadata(parsed, metadata)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, OverflowError, RecursionError) as exc:
            parse_error = f"{type(exc).__name__}: {exc}"[:1000]
            parsed = None
            timestamp = None
            kind = None
        records.append(
            _ParsedRecord(
                raw_bytes=raw_bytes,
                byte_offset=byte_offset,
                sha256=hashlib.sha256(raw_bytes).hexdigest(),
                parsed=parsed,
                parse_error=parse_error,
                timestamp=timestamp,
                kind=kind,
            )
        )
        byte_offset += len(raw_bytes)
        batch_bytes += len(raw_bytes)
    metadata["_byte_offset"] = byte_offset
    return records


def _reject_json_constant(token: str) -> None:
    raise ValueError(f"invalid JSON numeric constant {token}")


def _validate_jsonb(value: Any, path: str = "$", depth: int = 0) -> None:
    if depth > 200:
        raise ValueError(f"JSON nesting exceeds Postgres safety limit at {path}")
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError(f"JSONB cannot store NUL at {path}")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError(f"JSONB cannot store an unpaired surrogate at {path}") from exc
    elif isinstance(value, bool) or value is None:
        return
    elif isinstance(value, int):
        if len(str(abs(value))) > 131072:
            raise ValueError(f"JSONB numeric is too large at {path}")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"JSONB cannot store a non-finite number at {path}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_jsonb(item, f"{path}[{index}]", depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"JSON object key is not text at {path}")
            _validate_jsonb(key, f"{path}.<key>", depth + 1)
            _validate_jsonb(item, f"{path}.{key}", depth + 1)
    else:
        raise ValueError(f"Unsupported JSON value {type(value).__name__} at {path}")


def _record_timestamp(record: Mapping[str, Any]) -> datetime | None:
    for key in ("timestamp", "created_at", "createdAt", "ts"):
        value = record.get(key)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
                return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
            except ValueError:
                continue
    return None


def _record_kind(record: Mapping[str, Any]) -> str | None:
    for key in ("type", "kind", "event_type"):
        value = record.get(key)
        if isinstance(value, str):
            return value[:200]
    return None


def _extract_session_metadata(record: Mapping[str, Any], metadata: dict[str, Any]) -> None:
    payload = record.get("payload")
    data = payload if isinstance(payload, dict) else {}
    if record.get("type") == "session_meta":
        if isinstance(data.get("id"), str):
            _remember_metadata_value(metadata, "native_session_id", data["id"])
        if isinstance(data.get("parent_thread_id"), str):
            _remember_metadata_value(metadata, "parent_native_session_id", data["parent_thread_id"])
    for candidate in (record.get("cwd"), data.get("cwd")):
        if isinstance(candidate, str):
            metadata.setdefault("cwd", candidate)
            break
    timestamp = _record_timestamp(record)
    if timestamp is not None:
        first = metadata.get("first_timestamp")
        last = metadata.get("last_timestamp")
        metadata["first_timestamp"] = timestamp if first is None else min(first, timestamp)
        metadata["last_timestamp"] = timestamp if last is None else max(last, timestamp)
    if metadata.get("title"):
        return
    title = record.get("title") or record.get("summary")
    if isinstance(title, str) and title.strip():
        metadata["title"] = title.strip()[:500]
        return
    message = record.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str) and content.strip() and record.get("type") == "user":
            metadata["title"] = content.strip()[:500]
        elif isinstance(content, list) and record.get("type") == "user":
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip():
                    metadata["title"] = part["text"].strip()[:500]
                    break


def _remember_metadata_value(metadata: dict[str, Any], key: str, value: str) -> None:
    candidates = metadata.setdefault(f"{key}_candidates", [])
    if value not in candidates:
        candidates.append(value)
    if len(candidates) > 1:
        metadata[f"{key}_conflict"] = True
    metadata.setdefault(key, value)


def _version_lineage_ranks(db: DbSession, version: FileVersionRow) -> dict[UUID, int]:
    lineage = [version]
    while not lineage[-1].is_checkpoint:
        parent_id = lineage[-1].parent_version_id
        if parent_id is None or len(lineage) > CHECKPOINT_INTERVAL:
            raise RuntimeError(f"invalid file version delta chain at {lineage[-1].id}")
        parent = db.get(FileVersionRow, parent_id)
        if parent is None or parent.session_id != version.session_id:
            raise RuntimeError(f"file version has an invalid parent: {lineage[-1].id}")
        lineage.append(parent)
    return {row.id: rank for rank, row in enumerate(lineage)}


def _effective_payload_ids(
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
