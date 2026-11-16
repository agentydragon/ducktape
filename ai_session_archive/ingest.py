"""Idempotent Restic snapshot ingestion for local Claude and Codex JSONL."""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, BinaryIO, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import and_, delete, exists, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session as DbSession, aliased

from ai_session_archive import file_publication
from ai_session_archive.claude_title_refresh import refresh_claude_titles
from ai_session_archive.codex_metadata import read_codex_titles
from ai_session_archive.database import (
    CodexTitleCandidateRow,
    CodexTitleObservationRow,
    FileVersionImportRow,
    FileVersionRecordRow,
    FileVersionRow,
    MachineRow,
    RecordPayloadRow,
    ResticSnapshotRow,
    SessionRootRow,
    SessionRow,
    SnapshotFileRow,
)
from ai_session_archive.file_publication import SnapshotFileIdentity
from ai_session_archive.file_staging import FileStaging
from ai_session_archive.models import Harness, SessionKind
from ai_session_archive.record_interpretation import extract_session_metadata, session_kind_from_record
from ai_session_archive.record_json import ParsedRecordJson, parse_record_json
from ai_session_archive.record_payloads import PARSER_VERSION, ParsedRecord
from ai_session_archive.restic import ResticRepository, ResticSnapshot
from ai_session_archive.snapshot_inventory import (
    KNOWN_DIFF_MODIFIERS,
    SnapshotInventory,
    assert_snapshot_identity,
    error_text,
)
from ai_session_archive.source_paths import HarnessRoot, SourceLocation, classify_source_path
from ai_session_archive.statuses import (
    CodexMetadataStatus,
    SnapshotFileStatus,
    SnapshotInventoryStatus,
    SnapshotProcessingStatus,
)
from ai_session_archive.usage_observations import usage_observations_for_record
from ai_session_archive.version_lineage import CHECKPOINT_INTERVAL, version_lineage_ranks

RECORD_BATCH_MAX_BYTES = 4 * 1024 * 1024


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


class SnapshotIngestor:
    """Imports all retained snapshots for one Restic repository into Postgres."""

    def __init__(self, session_factory: Callable[[], DbSession], source: ArchiveSource):
        self._session_factory = session_factory
        self._staging = FileStaging(session_factory)
        self._source = source
        self._inventory = SnapshotInventory(
            session_factory=session_factory,
            machine_id=source.machine_id,
            restic=source.restic,
            roots=source.roots,
            ensure_session=self._ensure_session,
        )

    def ingest(self) -> IngestReport:
        snapshots = sorted(self._source.restic.list_snapshots(), key=lambda row: (row.captured_at, row.id))
        self._ensure_source()
        self._inventory.discover(snapshots)
        self._project_missing_session_kinds()
        available_snapshot_ids = {snapshot.id for snapshot in snapshots}
        if snapshots:
            # Make the latest names visible before a potentially long historical transcript import.
            self._process_codex_metadata(snapshots[-1], refresh_completed=True)
            self._project_codex_titles(allow_index_only=False)

        snapshot_states = self._inventory.states(available_snapshot_ids)
        for snapshot in snapshots:
            inventory_status, processing_status, metadata_status = snapshot_states[snapshot.id]
            if inventory_status != SnapshotInventoryStatus.COMPLETE:
                self._inventory.prepare(snapshot, available_snapshot_ids)
            if metadata_status not in (CodexMetadataStatus.COMPLETE, CodexMetadataStatus.PARTIAL):
                self._process_codex_metadata(snapshot)
            if (
                inventory_status != SnapshotInventoryStatus.COMPLETE
                or processing_status != SnapshotProcessingStatus.COMPLETE
            ):
                self._process_snapshot(snapshot)
        all_inventoried = self._inventory.all_inventoried_and_processed(available_snapshot_ids)
        self._project_codex_titles(allow_index_only=all_inventoried)
        refresh_claude_titles(self._session_factory, self._source.machine_id)
        return self._report(available_snapshot_ids)

    def _ensure_source(self) -> None:
        with self._session_factory() as db, db.begin():
            db.execute(
                pg_insert(MachineRow)
                .values(id=self._source.machine_id, display_name=self._source.display_name)
                .on_conflict_do_update(index_elements=[MachineRow.id], set_={"display_name": self._source.display_name})
            )
            configured = {
                (row.harness, row.root_path)
                for row in db.scalars(
                    select(SessionRootRow).where(SessionRootRow.machine_id == self._source.machine_id)
                )
            }
            for root in self._inventory.roots:
                key = (root.harness.value, str(root.path))
                if key not in configured:
                    db.add(SessionRootRow(machine_id=self._source.machine_id, harness=key[0], root_path=key[1]))
            # A legacy importer can publish content while a newer importer has staged it.
            db.execute(
                delete(FileVersionImportRow).where(
                    FileVersionImportRow.session_id.in_(
                        select(SessionRow.id).where(SessionRow.machine_id == self._source.machine_id)
                    ),
                    exists().where(
                        FileVersionRow.session_id == FileVersionImportRow.session_id,
                        FileVersionRow.sha256 == FileVersionImportRow.sha256,
                    ),
                )
            )

    def _ensure_session(self, db: DbSession, location: SourceLocation, inferred: bool = False) -> SessionRow:
        # Inventories for distinct snapshots can discover the same physical session concurrently.
        db.get(MachineRow, self._source.machine_id, with_for_update=True)
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
            parent_location = classify_source_path(location.parent_source_path, self._inventory.roots)
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
            if file_row.status not in (SnapshotFileStatus.PENDING, SnapshotFileStatus.FAILED):
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
            if snapshot_row.inventory_status != SnapshotInventoryStatus.COMPLETE:
                snapshot_row.processing_status = SnapshotProcessingStatus.FAILED
                return
            statuses = list(
                db.scalars(
                    select(SnapshotFileRow.status).where(
                        SnapshotFileRow.machine_id == self._source.machine_id,
                        SnapshotFileRow.snapshot_id == snapshot.id,
                    )
                )
            )
            failed = any(status in (SnapshotFileStatus.PENDING, SnapshotFileStatus.FAILED) for status in statuses)
            snapshot_row.processing_status = (
                SnapshotProcessingStatus.FAILED if failed else SnapshotProcessingStatus.COMPLETE
            )

    def _process_codex_metadata(self, snapshot: ResticSnapshot, *, refresh_completed: bool = False) -> None:
        refresh_kinds = False
        with self._session_factory() as db, db.begin():
            row = db.get(ResticSnapshotRow, (self._source.machine_id, snapshot.id))
            if row is None:
                row = self._inventory.new_snapshot_row(snapshot)
                db.add(row)
            if row.codex_metadata_status in (CodexMetadataStatus.COMPLETE, CodexMetadataStatus.PARTIAL):
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
        roots = tuple(root.path for root in self._inventory.roots if root.harness == Harness.CODEX)
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
                if row.codex_metadata_status in (CodexMetadataStatus.COMPLETE, CodexMetadataStatus.PARTIAL):
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
                row.codex_metadata_status = (
                    CodexMetadataStatus.FAILED
                    if retry_errors
                    else CodexMetadataStatus.PARTIAL
                    if warnings
                    else CodexMetadataStatus.COMPLETE
                )
                issues = [*retry_errors, *warnings]
                row.codex_metadata_error = "; ".join(issues)[:4000] if issues else None
        except Exception as exc:
            with self._session_factory() as db, db.begin():
                row = db.get(ResticSnapshotRow, (self._source.machine_id, snapshot.id), with_for_update=True)
                if row is not None and not refresh_kinds:
                    row.codex_metadata_status = CodexMetadataStatus.FAILED
                    row.codex_metadata_error = error_text(exc)

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
            latest_kinds: dict[UUID, tuple[datetime, str, str]] = {}
            for candidate in db.scalars(
                select(CodexTitleCandidateRow).where(CodexTitleCandidateRow.machine_id == self._source.machine_id)
            ):
                match = by_path.get(candidate.source_path) if candidate.source_path else None
                if match is not None and match.native_session_id != candidate.native_session_id:
                    continue
                if match is None and not candidate.source_path and allow_index_only:
                    matches = by_native.get(candidate.native_session_id, [])
                    match = matches[0] if len(matches) == 1 else None
                if match is None:
                    continue
                rank = (candidate.captured_at, candidate.snapshot_id, candidate.value)
                target = latest if candidate.field == "title" else latest_kinds
                if match.id not in target or rank > target[match.id]:
                    target[match.id] = rank
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
                        kind = session_kind_from_record(Harness(row.harness), parsed.parsed)
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
                snapshot_row = db.scalar(
                    select(ResticSnapshotRow)
                    .where(
                        ResticSnapshotRow.machine_id == self._source.machine_id,
                        ResticSnapshotRow.snapshot_id == snapshot.id,
                    )
                    .with_for_update()
                )
                if snapshot_row is None:
                    raise RuntimeError(f"missing snapshot row for {snapshot.id}")
                assert_snapshot_identity(snapshot_row, snapshot)
                row = db.scalar(
                    select(SnapshotFileRow)
                    .where(
                        SnapshotFileRow.machine_id == self._source.machine_id,
                        SnapshotFileRow.snapshot_id == snapshot.id,
                        SnapshotFileRow.path == path,
                    )
                    .with_for_update()
                )
                if row is None or row.status not in (SnapshotFileStatus.PENDING, SnapshotFileStatus.FAILED):
                    return
                location = classify_source_path(path, self._inventory.roots)
                if location is None:
                    row.status = SnapshotFileStatus.UNCLASSIFIED
                    return
                if row.change_kind == "-":
                    row.status = SnapshotFileStatus.DELETED
                    return
                if any(part not in KNOWN_DIFF_MODIFIERS and part != "inventory" for part in row.change_kind.split(",")):
                    raise ValueError(f"unrecognized Restic diff modifier: {row.change_kind}")
                file_identity = SnapshotFileIdentity.from_row(row)
                inventory_status = snapshot_row.inventory_status
                comparison_snapshot_id = snapshot_row.comparison_snapshot_id

            with cast(BinaryIO, tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")) as contents:
                self._source.restic.dump_to(snapshot.id, path, contents)
                contents.seek(0)
                sha256, byte_size, record_count = _fingerprint_file(contents)
                contents.seek(0)

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
                        raise RuntimeError(f"snapshot row disappeared for {snapshot.id}")
                    assert_snapshot_identity(snapshot_row, snapshot)
                    row = db.scalar(
                        select(SnapshotFileRow)
                        .where(
                            SnapshotFileRow.machine_id == self._source.machine_id,
                            SnapshotFileRow.snapshot_id == snapshot.id,
                            SnapshotFileRow.path == path,
                        )
                        .with_for_update()
                    )
                    if row is None or row.status not in (SnapshotFileStatus.PENDING, SnapshotFileStatus.FAILED):
                        return
                    if SnapshotFileIdentity.from_row(row) != file_identity:
                        raise RuntimeError(f"snapshot file identity changed while reading {path}")
                    if (
                        snapshot_row.inventory_status != inventory_status
                        or snapshot_row.comparison_snapshot_id != comparison_snapshot_id
                    ):
                        # Inventory may have retried while Restic was reading. Leave this row eligible
                        # for the next pass, which will capture the new comparison-parent decision.
                        return
                    session_row = (
                        db.get(SessionRow, row.session_id) if row.session_id else self._ensure_session(db, location)
                    )
                    if session_row is None:
                        raise RuntimeError(f"missing session row for {path}")
                    session_id = session_row.id
                self._store_version(
                    snapshot=snapshot,
                    path=path,
                    location=location,
                    session_id=session_id,
                    sha256=sha256,
                    byte_size=byte_size,
                    record_count=record_count,
                    contents=contents,
                    file_identity=file_identity,
                    inventory_status=inventory_status,
                    comparison_snapshot_id=comparison_snapshot_id,
                )

        except Exception as exc:
            self._mark_file_failed(snapshot.id, path, exc)

    def _store_version(
        self,
        *,
        snapshot: ResticSnapshot,
        path: str,
        location: SourceLocation,
        session_id: UUID,
        sha256: str,
        byte_size: int,
        record_count: int,
        contents: BinaryIO,
        file_identity: SnapshotFileIdentity,
        inventory_status: str,
        comparison_snapshot_id: str | None,
    ) -> None:
        version_id = uuid5(session_id, f"{PARSER_VERSION}:{sha256}")
        parent = None
        parent_ranks: dict[UUID, int] = {}
        with self._session_factory() as db, db.begin():
            existing = db.scalar(
                select(FileVersionRow).where(FileVersionRow.session_id == session_id, FileVersionRow.sha256 == sha256)
            )
            if existing is not None and (existing.byte_size != byte_size or existing.parser_version != PARSER_VERSION):
                raise RuntimeError("file content hash collides with an incompatible stored version")
            if existing is None:
                snapshot_file = db.get(SnapshotFileRow, (self._source.machine_id, snapshot.id, path))
                assert snapshot_file is not None
                previous = file_publication.previous_version(db, snapshot_file)
                db.execute(
                    pg_insert(FileVersionImportRow)
                    .values(
                        id=version_id,
                        session_id=session_id,
                        sha256=sha256,
                        parser_version=PARSER_VERSION,
                        byte_size=byte_size,
                        record_count=record_count,
                        imported_record_count=0,
                        parse_issue_count=0,
                        parent_version_id=previous.id if previous is not None else None,
                        shared_prefix_record_count=0,
                    )
                    .on_conflict_do_nothing()
                )
                stage = db.get(FileVersionImportRow, version_id)
                assert stage is not None
                if (
                    stage.byte_size != byte_size
                    or stage.record_count != record_count
                    or stage.parser_version != PARSER_VERSION
                ):
                    raise RuntimeError("file content hash collides with an incompatible staged version")
                if stage.parent_version_id is not None:
                    parent = db.get(FileVersionRow, stage.parent_version_id)
                    assert parent is not None
                    if parent.delta_depth + 1 >= CHECKPOINT_INTERVAL:
                        parent = None
                    else:
                        # Published lineage is immutable; avoid traversing it again for every batch.
                        parent_ranks = version_lineage_ranks(db, parent)
        metadata: dict[str, Any] = {}
        ordinal = 0
        if existing is None:
            # Parsing, usage projection and metadata extraction run without a database transaction.
            while batch := _read_record_batch(contents, metadata, limit=500, harness=location.harness):
                self._staging.store_record_batch(version_id, ordinal, batch, parent=parent, parent_ranks=parent_ranks)
                ordinal += len(batch)
            if ordinal != record_count:
                raise RuntimeError(f"file changed while parsing: expected {record_count} JSONL records, read {ordinal}")
            metadata.setdefault("session_kind", SessionKind.UNKNOWN.value)
        file_publication.publish_file_version(
            session_factory=self._session_factory,
            machine_id=self._source.machine_id,
            snapshot=snapshot,
            path=path,
            location=location,
            version_id=version_id,
            session_id=session_id,
            sha256=sha256,
            byte_size=byte_size,
            record_count=record_count,
            metadata=metadata,
            file_identity=file_identity,
            inventory_status=inventory_status,
            comparison_snapshot_id=comparison_snapshot_id,
        )

    def _mark_file_failed(self, snapshot_id: str, path: str, exc: Exception) -> None:
        with self._session_factory() as db, db.begin():
            row = db.scalar(
                select(SnapshotFileRow)
                .where(
                    SnapshotFileRow.machine_id == self._source.machine_id,
                    SnapshotFileRow.snapshot_id == snapshot_id,
                    SnapshotFileRow.path == path,
                )
                .with_for_update()
            )
            if row is not None and row.status in (SnapshotFileStatus.PENDING, SnapshotFileStatus.FAILED):
                row.status = SnapshotFileStatus.FAILED
                row.error = error_text(exc)

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
                row.inventory_status == SnapshotInventoryStatus.COMPLETE
                and row.processing_status == SnapshotProcessingStatus.COMPLETE
                and row.codex_metadata_status in (CodexMetadataStatus.COMPLETE, CodexMetadataStatus.PARTIAL)
                for row in snapshots
            ),
            snapshots_incomplete=sum(
                row.inventory_status != SnapshotInventoryStatus.COMPLETE
                or row.processing_status != SnapshotProcessingStatus.COMPLETE
                or row.codex_metadata_status not in (CodexMetadataStatus.COMPLETE, CodexMetadataStatus.PARTIAL)
                for row in snapshots
            ),
            files_complete=sum(row.status == SnapshotFileStatus.COMPLETE for row in files),
            files_partial=sum(row.status == SnapshotFileStatus.PARTIAL for row in files),
            files_failed=sum(row.status == SnapshotFileStatus.FAILED for row in files),
            files_unclassified=sum(row.status == SnapshotFileStatus.UNCLASSIFIED for row in files),
            codex_metadata_partial=sum(row.codex_metadata_status == CodexMetadataStatus.PARTIAL for row in snapshots),
        )


def _session_id(machine_id: str, harness: Harness, source_path: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"ai-session:{machine_id}:{harness.value}:{source_path}")


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
) -> list[ParsedRecord]:
    records: list[ParsedRecord] = []
    batch_bytes = 0
    byte_offset = int(metadata.get("_byte_offset", 0))
    while len(records) < limit and batch_bytes < RECORD_BATCH_MAX_BYTES and (raw_bytes := source.readline()):
        parsed = parse_record_json(raw_bytes)
        if isinstance(parsed, ParsedRecordJson) and isinstance(parsed.parsed, Mapping):
            extract_session_metadata(parsed.parsed, metadata, timestamp=parsed.timestamp, harness=harness)
        records.append(
            ParsedRecord(
                raw_bytes=raw_bytes,
                byte_offset=byte_offset,
                sha256=hashlib.sha256(raw_bytes).hexdigest(),
                json=parsed,
                usage=tuple(usage_observations_for_record(parsed.parsed))
                if isinstance(parsed, ParsedRecordJson)
                else (),
            )
        )
        byte_offset += len(raw_bytes)
        batch_bytes += len(raw_bytes)
    metadata["_byte_offset"] = byte_offset
    return records
