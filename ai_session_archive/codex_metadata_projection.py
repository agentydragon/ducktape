"""Refresh and project Codex snapshot metadata into archive sessions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import PurePosixPath
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session as DbSession

from ai_session_archive.codex_metadata import read_codex_titles
from ai_session_archive.database import CodexTitleCandidateRow, CodexTitleObservationRow, ResticSnapshotRow, SessionRow
from ai_session_archive.models import Harness, SessionKind
from ai_session_archive.restic import ResticRepository, ResticSnapshot
from ai_session_archive.snapshot_inventory import error_text
from ai_session_archive.statuses import CodexMetadataStatus


class CodexMetadataProjector:
    """Own Codex metadata refresh decisions and current title/kind projection."""

    def __init__(
        self,
        session_factory: Callable[[], DbSession],
        machine_id: str,
        restic: ResticRepository,
        codex_roots: tuple[PurePosixPath, ...],
        new_snapshot_row: Callable[[ResticSnapshot], ResticSnapshotRow],
    ) -> None:
        self._session_factory = session_factory
        self._machine_id = machine_id
        self._restic = restic
        self._codex_roots = codex_roots
        self._new_snapshot_row = new_snapshot_row

    def process_snapshot(self, snapshot: ResticSnapshot, *, refresh_completed: bool = False) -> None:
        refresh_kinds = False
        with self._session_factory() as db, db.begin():
            row = db.get(ResticSnapshotRow, (self._machine_id, snapshot.id))
            if row is None:
                row = self._new_snapshot_row(snapshot)
                db.add(row)
            if row.codex_metadata_status in (CodexMetadataStatus.COMPLETE, CodexMetadataStatus.PARTIAL):
                if not refresh_completed:
                    return
                refresh_kinds = bool(
                    db.scalar(
                        select(
                            exists().where(
                                CodexTitleObservationRow.machine_id == self._machine_id,
                                CodexTitleObservationRow.snapshot_id == snapshot.id,
                                CodexTitleObservationRow.session_kind.is_(None),
                            )
                        )
                    )
                )
                if not refresh_kinds:
                    return
        try:
            observations, warnings, retry_errors = read_codex_titles(self._restic, snapshot.id, self._codex_roots)
            with self._session_factory() as db, db.begin():
                row = db.get(ResticSnapshotRow, (self._machine_id, snapshot.id), with_for_update=True)
                if row is None:
                    return
                existing_observations = {
                    observation.native_session_id: observation
                    for observation in db.scalars(
                        select(CodexTitleObservationRow).where(
                            CodexTitleObservationRow.machine_id == self._machine_id,
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
                                machine_id=self._machine_id,
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
                row = db.get(ResticSnapshotRow, (self._machine_id, snapshot.id), with_for_update=True)
                if row is not None and not refresh_kinds:
                    row.codex_metadata_status = CodexMetadataStatus.FAILED
                    row.codex_metadata_error = error_text(exc)

    def project_titles(self, *, allow_index_only: bool) -> None:
        with self._session_factory() as db, db.begin():
            sessions = list(
                db.scalars(
                    select(SessionRow).where(
                        SessionRow.machine_id == self._machine_id, SessionRow.harness == Harness.CODEX.value
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
            # Read the compact candidate table, never the observation history: wyrm2 holds millions of
            # observations for about 6,000 distinct (session, path) pairs, and a buffered ORM result over
            # them OOM-killed the 2 GiB importer.
            for candidate in db.scalars(
                select(CodexTitleCandidateRow).where(CodexTitleCandidateRow.machine_id == self._machine_id)
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
