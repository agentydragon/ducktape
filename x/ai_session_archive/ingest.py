"""Idempotent Restic snapshot ingestion for local Claude and Codex JSONL."""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, BinaryIO, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import and_, exists, literal, select, text
from sqlalchemy.orm import Session as DbSession, aliased, load_only

from x.ai_session_archive.claude_titles import ClaudeTitle, select_claude_title
from x.ai_session_archive.codex_metadata import classify_codex_source, read_codex_titles
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
    UsageObservationRow,
)
from x.ai_session_archive.models import Harness, SessionKind
from x.ai_session_archive.record_json import ParsedRecordJson, RecordJsonParseError, parse_record_json
from x.ai_session_archive.restic import ResticChange, ResticFile, ResticRepository, ResticSnapshot
from x.ai_session_archive.usage_observations import usage_observations_for_record

PARSER_VERSION = "jsonl-v1"
CLAUDE_TITLE_PROJECTION_VERSION = 1
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
    json: ParsedRecordJson | RecordJsonParseError


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
        self._project_missing_session_kinds()
        available_snapshot_ids = {snapshot.id for snapshot in snapshots}
        if snapshots:
            # Make the latest names visible before a potentially long historical transcript import.
            self._process_codex_metadata(snapshots[-1], refresh_completed=True)
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
        self._project_claude_titles()
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
            session_kind=(
                SessionKind.SUBAGENT.value
                if location.harness == Harness.CLAUDE and location.parent_source_path is not None
                else SessionKind.MAIN.value
                if location.harness == Harness.CLAUDE and inferred
                else SessionKind.UNKNOWN.value
            ),
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

    def _process_codex_metadata(self, snapshot: ResticSnapshot, *, refresh_completed: bool = False) -> None:
        refresh_kinds = False
        with self._session_factory() as db, db.begin():
            row = db.get(ResticSnapshotRow, (self._source.machine_id, snapshot.id))
            if row is None:
                row = self._new_snapshot_row(snapshot)
                db.add(row)
            if row.codex_metadata_status in ("complete", "partial"):
                if not refresh_completed:
                    return
                refresh_kinds = bool(
                    db.scalar(
                        select(
                            exists().where(
                                CodexTitleObservationRow.machine_id == self._source.machine_id,
                                CodexTitleObservationRow.snapshot_id == snapshot.id,
                                CodexTitleObservationRow.session_kind.is_(None),
                            )
                        )
                    )
                )
                if not refresh_kinds:
                    return
        roots = tuple(root.path for root in self._roots if root.harness == Harness.CODEX)
        try:
            observations, warnings, retry_errors = read_codex_titles(self._source.restic, snapshot.id, roots)
            with self._session_factory() as db, db.begin():
                row = db.get(ResticSnapshotRow, (self._source.machine_id, snapshot.id), with_for_update=True)
                if row is None:
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
                if refresh_kinds:
                    if not retry_errors:
                        refreshed = {item.native_session_id: item for item in observations}
                        for existing in existing_observations.values():
                            if existing.session_kind is None:
                                item = refreshed.get(existing.native_session_id)
                                existing.session_kind = (
                                    item.session_kind.value if item is not None else SessionKind.UNKNOWN.value
                                )
                    return
                if row.codex_metadata_status in ("complete", "partial"):
                    return
                for item in observations:
                    existing_observation = existing_observations.get(item.native_session_id)
                    if existing_observation is None:
                        db.add(
                            CodexTitleObservationRow(
                                machine_id=self._source.machine_id,
                                snapshot_id=snapshot.id,
                                native_session_id=item.native_session_id,
                                source_path=item.source_path,
                                title=item.title,
                                title_source=item.title_source,
                                updated_at=item.updated_at,
                                session_kind=item.session_kind.value,
                            )
                        )
                    else:
                        existing_observation.source_path = item.source_path
                        existing_observation.title = item.title
                        existing_observation.title_source = item.title_source
                        existing_observation.updated_at = item.updated_at
                        existing_observation.session_kind = item.session_kind.value
                row.codex_metadata_status = "failed" if retry_errors else "partial" if warnings else "complete"
                issues = [*retry_errors, *warnings]
                row.codex_metadata_error = "; ".join(issues)[:4000] if issues else None
        except Exception as exc:
            with self._session_factory() as db, db.begin():
                row = db.get(ResticSnapshotRow, (self._source.machine_id, snapshot.id), with_for_update=True)
                if row is not None and not refresh_kinds:
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
                .where(CodexTitleObservationRow.title.is_not(None))
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
                assert observation.title is not None
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

            latest_kinds: dict[UUID, tuple[datetime, str, str]] = {}
            kind_observations = db.execute(
                select(CodexTitleObservationRow, ResticSnapshotRow.captured_at)
                .join(
                    ResticSnapshotRow,
                    (ResticSnapshotRow.machine_id == CodexTitleObservationRow.machine_id)
                    & (ResticSnapshotRow.snapshot_id == CodexTitleObservationRow.snapshot_id),
                )
                .where(CodexTitleObservationRow.machine_id == self._source.machine_id)
                .where(CodexTitleObservationRow.session_kind.is_not(None))
                .distinct(CodexTitleObservationRow.native_session_id, CodexTitleObservationRow.source_path)
                .order_by(
                    CodexTitleObservationRow.native_session_id,
                    CodexTitleObservationRow.source_path,
                    ResticSnapshotRow.captured_at.desc(),
                    CodexTitleObservationRow.snapshot_id.desc(),
                )
            )
            for observation, captured_at in kind_observations:
                assert observation.session_kind is not None
                match = by_path.get(observation.source_path) if observation.source_path else None
                if match is not None and match.native_session_id != observation.native_session_id:
                    continue
                if match is None and not observation.source_path and allow_index_only:
                    candidates = by_native.get(observation.native_session_id, [])
                    match = candidates[0] if len(candidates) == 1 else None
                if match is None:
                    continue
                rank = (captured_at, observation.snapshot_id, observation.session_kind)
                if match.id not in latest_kinds or rank > latest_kinds[match.id]:
                    latest_kinds[match.id] = rank
            for row in sessions:
                if row.id in latest:
                    row.title = latest[row.id][2]
                    row.codex_title_from_metadata = True
                elif row.codex_title_from_metadata and allow_index_only:
                    row.title = None
                    row.codex_title_from_metadata = False
                if row.id in latest_kinds:
                    row.session_kind = latest_kinds[row.id][2]

    def _project_missing_session_kinds(self) -> None:
        with self._session_factory() as db, db.begin():
            latest_version_id = (
                select(FileVersionRow.id)
                .where(FileVersionRow.session_id == SessionRow.id)
                .order_by(FileVersionRow.last_seen_at.desc(), FileVersionRow.id.desc())
                .limit(1)
                .scalar_subquery()
            )

            missing_sessions = (
                select(SessionRow.id.label("session_id"), latest_version_id.label("latest_version_id"))
                .where(SessionRow.machine_id == self._source.machine_id, SessionRow.session_kind.is_(None))
                .cte("missing_session_kinds")
            )
            lineage = (
                select(
                    missing_sessions.c.session_id,
                    FileVersionRow.id.label("version_id"),
                    FileVersionRow.parent_version_id,
                    FileVersionRow.shared_prefix_record_count,
                    literal(0).label("depth"),
                )
                .select_from(missing_sessions)
                .join(FileVersionRow, FileVersionRow.id == missing_sessions.c.latest_version_id)
                .cte("session_kind_version_lineage", recursive=True)
            )
            parent_version = aliased(FileVersionRow)
            lineage = lineage.union_all(
                select(
                    lineage.c.session_id,
                    parent_version.id,
                    parent_version.parent_version_id,
                    parent_version.shared_prefix_record_count,
                    lineage.c.depth + 1,
                )
                .join(parent_version, parent_version.id == lineage.c.parent_version_id)
                .where(lineage.c.shared_prefix_record_count > 0, lineage.c.depth < CHECKPOINT_INTERVAL - 1)
            )
            first_payloads = (
                select(lineage.c.session_id, FileVersionRecordRow.payload_id)
                .select_from(lineage)
                .join(
                    FileVersionRecordRow,
                    and_(
                        FileVersionRecordRow.file_version_id == lineage.c.version_id, FileVersionRecordRow.ordinal == 0
                    ),
                )
                .where(lineage.c.shared_prefix_record_count == 0)
                .cte("session_kind_first_payloads")
            )
            rows = db.execute(
                select(SessionRow, RecordPayloadRow.raw_bytes)
                .select_from(SessionRow)
                .outerjoin(first_payloads, first_payloads.c.session_id == SessionRow.id)
                .outerjoin(RecordPayloadRow, RecordPayloadRow.id == first_payloads.c.payload_id)
                .where(SessionRow.machine_id == self._source.machine_id, SessionRow.session_kind.is_(None))
                .order_by(SessionRow.id)
            )
            for row, raw_bytes in rows.yield_per(32):
                kind = None
                if raw_bytes is not None:
                    parsed = parse_record_json(raw_bytes)
                    if isinstance(parsed, ParsedRecordJson) and isinstance(parsed.parsed, Mapping):
                        kind = _session_kind_from_record(Harness(row.harness), parsed.parsed)
                if (
                    kind is None
                    and row.harness == Harness.CLAUDE.value
                    and row.parent_link_kind == "claude_path_inferred"
                ):
                    kind = SessionKind.SUBAGENT
                row.session_kind = (kind or SessionKind.UNKNOWN).value

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
        while batch := _read_record_batch(contents, metadata, limit=500, harness=location.harness):
            payloads = self._get_payloads(db, batch)
            parent_ids = (
                _effective_payload_ids(db, previous, parent_ranks, ordinal, ordinal + len(batch))
                if matching_prefix and previous is not None
                else []
            )
            additions = []
            for index, (record, payload) in enumerate(zip(batch, payloads, strict=True)):
                if isinstance(record.json, RecordJsonParseError):
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
        metadata.setdefault("session_kind", SessionKind.UNKNOWN.value)
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
                parse_error=record.json.parse_error if isinstance(record.json, RecordJsonParseError) else None,
                timestamp=record.json.timestamp if isinstance(record.json, ParsedRecordJson) else None,
                kind=record.json.kind if isinstance(record.json, ParsedRecordJson) else None,
            )
            db.add(payload)
            payload_by_hash[record.sha256] = payload
            if isinstance(record.json, ParsedRecordJson):
                for observation_index, observation in enumerate(usage_observations_for_record(record.json.parsed)):
                    db.add(
                        UsageObservationRow(
                            payload_id=payload.id,
                            observation_index=observation_index,
                            source_field=observation.source_field,
                            counter_kind=observation.counter_kind,
                            model=observation.model,
                            response_id=observation.response_id,
                            message_id=observation.message_id,
                            turn_id=observation.turn_id,
                            input_tokens=observation.input_tokens,
                            output_tokens=observation.output_tokens,
                            cache_read_input_tokens=observation.cache_read_input_tokens,
                            cache_write_input_tokens=observation.cache_write_input_tokens,
                            reasoning_output_tokens=observation.reasoning_output_tokens,
                            total_tokens=observation.total_tokens,
                        )
                    )
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
                    JOIN LATERAL (
                        SELECT snapshot_id, parent_snapshot_id, comparison_snapshot_id
                        FROM restic_snapshots
                        WHERE machine_id = :machine_id AND snapshot_id = ancestors.parent_snapshot_id
                        LIMIT 1
                    ) AS parent ON true
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
        session_kind = metadata.get("session_kind")
        if (
            isinstance(session_kind, str)
            and session_kind in {kind.value for kind in SessionKind}
            and (session_kind != SessionKind.UNKNOWN.value or row.session_kind is None)
        ):
            row.session_kind = session_kind
        # An unchanged known version returns no metadata; a parsed version replaces the projection.
        if row.harness == Harness.CLAUDE.value and metadata:
            title: ClaudeTitle | None = metadata.get("claude_title")
            row.title = title.text if title is not None else None
            row.claude_title_projection_version = CLAUDE_TITLE_PROJECTION_VERSION
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
        observations = (
            select(CodexTitleObservationRow)
            .join(
                ResticSnapshotRow,
                (ResticSnapshotRow.machine_id == CodexTitleObservationRow.machine_id)
                & (ResticSnapshotRow.snapshot_id == CodexTitleObservationRow.snapshot_id),
            )
            .where(
                CodexTitleObservationRow.machine_id == row.machine_id,
                CodexTitleObservationRow.native_session_id == row.native_session_id,
                CodexTitleObservationRow.source_path == row.source_path,
            )
            .order_by(ResticSnapshotRow.captured_at.desc(), CodexTitleObservationRow.snapshot_id.desc())
        )
        title, kind = db.execute(
            select(
                observations.with_only_columns(CodexTitleObservationRow.title)
                .where(CodexTitleObservationRow.title.is_not(None))
                .limit(1)
                .scalar_subquery(),
                observations.with_only_columns(CodexTitleObservationRow.session_kind)
                .where(CodexTitleObservationRow.session_kind.is_not(None))
                .limit(1)
                .scalar_subquery(),
            )
        ).one()
        if title is not None:
            row.title = title
            row.codex_title_from_metadata = True
        if kind is not None:
            row.session_kind = kind

    def _project_claude_titles(self) -> None:
        """Refresh stale Claude title projections from already stored record payloads."""
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
                SnapshotFileRow.status.in_(("complete", "partial")),
                SnapshotFileRow.file_version_id.is_not(None),
            )
            .order_by(ResticSnapshotRow.captured_at.desc(), ResticSnapshotRow.snapshot_id.desc())
            .limit(1)
            .scalar_subquery()
        )
        with self._session_factory() as db, db.begin():
            rows = db.execute(
                select(SessionRow, FileVersionRow)
                .join(FileVersionRow, FileVersionRow.id == latest_version_id)
                .where(
                    SessionRow.machine_id == self._source.machine_id,
                    SessionRow.harness == Harness.CLAUDE.value,
                    (SessionRow.claude_title_projection_version.is_(None))
                    | (SessionRow.claude_title_projection_version != CLAUDE_TITLE_PROJECTION_VERSION),
                )
                .order_by(SessionRow.id)
            )
            for session_row, version_row in rows.yield_per(8):
                title: ClaudeTitle | None = None
                ranks = _version_lineage_ranks(db, version_row)
                for start in range(0, version_row.record_count, 500):
                    batch_ids = _effective_payload_ids(
                        db, version_row, ranks, start, min(start + 500, version_row.record_count)
                    )
                    raw_by_id = dict(
                        db.execute(
                            select(RecordPayloadRow.id, RecordPayloadRow.raw_bytes).where(
                                RecordPayloadRow.id.in_(batch_ids)
                            )
                        ).all()
                    )
                    for payload_id in batch_ids:
                        raw_bytes = raw_by_id[payload_id]
                        parsed = parse_record_json(raw_bytes)
                        if isinstance(parsed, ParsedRecordJson) and isinstance(parsed.parsed, Mapping):
                            title = select_claude_title(title, parsed.parsed)
                session_row.title = title.text if title is not None else None
                session_row.claude_title_projection_version = CLAUDE_TITLE_PROJECTION_VERSION

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


def _read_record_batch(
    source: BinaryIO, metadata: dict[str, Any], *, limit: int, harness: Harness
) -> list[_ParsedRecord]:
    records: list[_ParsedRecord] = []
    batch_bytes = 0
    byte_offset = int(metadata.get("_byte_offset", 0))
    while len(records) < limit and batch_bytes < RECORD_BATCH_MAX_BYTES and (raw_bytes := source.readline()):
        parsed = parse_record_json(raw_bytes)
        if isinstance(parsed, ParsedRecordJson) and isinstance(parsed.parsed, Mapping):
            _extract_session_metadata(parsed.parsed, metadata, timestamp=parsed.timestamp, harness=harness)
        records.append(
            _ParsedRecord(
                raw_bytes=raw_bytes, byte_offset=byte_offset, sha256=hashlib.sha256(raw_bytes).hexdigest(), json=parsed
            )
        )
        byte_offset += len(raw_bytes)
        batch_bytes += len(raw_bytes)
    metadata["_byte_offset"] = byte_offset
    return records


def _extract_session_metadata(
    record: Mapping[str, Any], metadata: dict[str, Any], *, timestamp: datetime | None, harness: Harness
) -> None:
    payload = record.get("payload")
    data = payload if isinstance(payload, dict) else {}
    if record.get("type") == "session_meta":
        if isinstance(data.get("id"), str):
            _remember_metadata_value(metadata, "native_session_id", data["id"])
        if isinstance(data.get("parent_thread_id"), str):
            _remember_metadata_value(metadata, "parent_native_session_id", data["parent_thread_id"])
        if harness == Harness.CODEX:
            _remember_session_kind(metadata, classify_codex_source(data.get("source")))
    if harness == Harness.CLAUDE and isinstance(record.get("isSidechain"), bool):
        _remember_session_kind(metadata, SessionKind.SUBAGENT if record["isSidechain"] else SessionKind.MAIN)
    for candidate in (record.get("cwd"), data.get("cwd")):
        if isinstance(candidate, str):
            metadata.setdefault("cwd", candidate)
            break
    if timestamp is not None:
        first = metadata.get("first_timestamp")
        last = metadata.get("last_timestamp")
        metadata["first_timestamp"] = timestamp if first is None else min(first, timestamp)
        metadata["last_timestamp"] = timestamp if last is None else max(last, timestamp)
    metadata["claude_title"] = select_claude_title(metadata.get("claude_title"), record)


def _session_kind_from_record(harness: Harness, record: Mapping[str, Any]) -> SessionKind | None:
    if harness == Harness.CODEX and record.get("type") == "session_meta":
        payload = record.get("payload")
        data = payload if isinstance(payload, Mapping) else {}
        return classify_codex_source(data.get("source"))
    if harness == Harness.CLAUDE and isinstance(record.get("isSidechain"), bool):
        return SessionKind.SUBAGENT if record["isSidechain"] else SessionKind.MAIN
    return None


def _remember_session_kind(metadata: dict[str, Any], kind: SessionKind) -> None:
    if metadata.get("session_kind_conflict"):
        return
    existing = metadata.get("session_kind")
    if kind == SessionKind.UNKNOWN:
        metadata.setdefault("session_kind", SessionKind.UNKNOWN.value)
        return
    if existing is None or existing == SessionKind.UNKNOWN.value:
        metadata["session_kind"] = kind.value
    elif existing != kind.value:
        metadata["session_kind"] = SessionKind.UNKNOWN.value
        metadata["session_kind_conflict"] = True


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
