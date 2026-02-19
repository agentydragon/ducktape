"""Sync specimens to database."""

from __future__ import annotations

import hashlib
import io
import logging
import tarfile
from collections.abc import Callable, Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel
from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from props.core.ids import SnapshotSlug
from props.core.models.true_positive import FalsePositiveOccurrence, LineRange, TruePositiveOccurrence
from props.core.splits import Split
from props.db.models import (
    CriticScopeExpectedToRecall,
    FalsePositive,
    FalsePositiveOccurrenceORM,
    FalsePositiveRelevantFileORM,
    FileSet,
    FileSetMember,
    OccurrenceRangeORM,
    Snapshot,
    SnapshotFile,
    TruePositive,
    TruePositiveOccurrenceORM,
)
from props.db.sync.stats import SyncStats
from props.db.sync.yaml_loader import SyncFalsePositive, SyncTruePositive, YAMLIssue

logger = logging.getLogger(__name__)


class SpecimenData(BaseModel):
    """Specimen data YAML blob structure."""

    snapshot_slug: SnapshotSlug
    split: Split
    issues: dict[str, YAMLIssue]


@dataclass
class SpecimenBundle:
    """Bundle artifacts for a single specimen."""

    code_tar: Path
    data: SpecimenData

    @property
    def slug(self) -> SnapshotSlug:
        """Snapshot slug from parsed data."""
        return self.data.snapshot_slug

    @staticmethod
    def from_paths(code_tar: Path, data_yaml: Path) -> SpecimenBundle:
        """Create a SpecimenBundle from code tar and data YAML paths."""
        with data_yaml.open() as f:
            specimen_data = SpecimenData.model_validate(yaml.safe_load(f))
        return SpecimenBundle(code_tar=code_tar, data=specimen_data)


def _add_ranges_to_occurrence(
    orm_occ: TruePositiveOccurrenceORM | FalsePositiveOccurrenceORM, files: dict[Path, list[LineRange] | None]
) -> None:
    """Add OccurrenceRangeORM objects to an occurrence from a files dict."""
    tp_id = orm_occ.tp_id if isinstance(orm_occ, TruePositiveOccurrenceORM) else None
    fp_id = orm_occ.fp_id if isinstance(orm_occ, FalsePositiveOccurrenceORM) else None
    for file_path, ranges in files.items():
        if ranges is None:
            continue
        for range_id, line_range in enumerate(ranges):
            orm_occ.ranges.append(
                OccurrenceRangeORM(
                    snapshot_slug=orm_occ.snapshot_slug,
                    tp_id=tp_id,
                    fp_id=fp_id,
                    occurrence_id=orm_occ.occurrence_id,
                    file_path=file_path,
                    range_id=range_id,
                    start_line=line_range.start_line,
                    end_line=line_range.end_line if line_range.end_line is not None else line_range.start_line,
                    note=line_range.note,
                )
            )


def sync_snapshot_files_to_db(session: Session, slug: SnapshotSlug, archive_bytes: bytes) -> SyncStats:
    """Sync snapshot_files table from a snapshot's code tar archive."""
    existing_keys: set[tuple[SnapshotSlug, str]] = {
        (row[0], row[1])
        for row in session.execute(
            select(SnapshotFile.snapshot_slug, SnapshotFile.file_path).where(SnapshotFile.snapshot_slug == slug)
        )
    }

    # Extract UTF-8 files from tar; skip directories and non-UTF-8 content.
    file_rows: list[dict] = []
    seen_keys: set[tuple[SnapshotSlug, str]] = set()
    buffer = io.BytesIO(archive_bytes)
    with tarfile.open(fileobj=buffer, mode="r") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            if (f := tar.extractfile(member)) is None:
                continue
            try:
                content = f.read().decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("Skipping non-UTF-8 file: %r in %s", member.name, slug)
                continue
            line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
            seen_keys.add((slug, member.name))
            file_rows.append({"snapshot_slug": slug, "file_path": member.name, "line_count": line_count})

    # Bulk upsert all files in one statement.
    stmt = insert(SnapshotFile).values(file_rows)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[SnapshotFile.snapshot_slug, SnapshotFile.file_path],
            set_={"line_count": stmt.excluded.line_count},
        )
    )

    # Bulk delete orphaned files in one query.
    orphaned = existing_keys - seen_keys
    session.query(SnapshotFile).filter(
        tuple_(SnapshotFile.snapshot_slug, SnapshotFile.file_path).in_(list(orphaned))
    ).delete(synchronize_session=False)

    added = len(seen_keys - existing_keys)
    updated = len(seen_keys & existing_keys)
    deleted = len(orphaned)
    total = len(seen_keys)
    session.flush()
    logger.info(f"Snapshot files synced: +{added} added, ~{updated} updated, -{deleted} deleted, ={total} total")
    return SyncStats(total=total, added=added, updated=updated, deleted=deleted)


def _reconstruct_occ_common(
    db_occ: TruePositiveOccurrenceORM | FalsePositiveOccurrenceORM,
) -> tuple[dict[Path, list[LineRange]], set[Path] | None]:
    """Reconstruct files dict and match_file_restriction from an ORM occurrence."""
    files: dict[Path, list[LineRange]] = {}
    for r in sorted(db_occ.ranges, key=lambda r: (str(r.file_path), r.range_id)):
        files.setdefault(Path(str(r.file_path)), []).append(
            LineRange(start_line=r.start_line, end_line=r.end_line, note=r.note)
        )

    restriction: set[Path] | None = None
    if db_occ.match_file_restriction is not None:
        session = Session.object_session(db_occ)
        assert session is not None
        members = (
            session.query(FileSetMember.file_path)
            .filter_by(snapshot_slug=db_occ.snapshot_slug, files_hash=db_occ.match_file_restriction)
            .all()
        )
        restriction = {Path(m.file_path) for m in members}
        if not restriction:
            raise ValueError(
                f"FileSet {db_occ.snapshot_slug}/{db_occ.match_file_restriction} has no members "
                f"(referenced by occurrence {db_occ.occurrence_id})"
            )

    return files, restriction


def _tp_occ_from_orm(db_occ: TruePositiveOccurrenceORM) -> TruePositiveOccurrence:
    """Reconstruct a YAML-equivalent Pydantic model from an ORM TP occurrence."""
    files, restriction = _reconstruct_occ_common(db_occ)
    return TruePositiveOccurrence(
        occurrence_id=db_occ.occurrence_id,
        files=files,
        note=db_occ.note,
        critic_scopes_expected_to_recall=db_occ.critic_scopes_expected_to_recall_set,
        match_file_restriction=restriction,
    )


def _fp_occ_from_orm(db_occ: FalsePositiveOccurrenceORM) -> FalsePositiveOccurrence:
    """Reconstruct a YAML-equivalent Pydantic model from an ORM FP occurrence."""
    files, restriction = _reconstruct_occ_common(db_occ)
    return FalsePositiveOccurrence(
        occurrence_id=db_occ.occurrence_id,
        files=files,
        note=db_occ.note,
        relevant_files={Path(str(rf.file_path)) for rf in db_occ.relevant_file_orms},
        match_file_restriction=restriction,
    )


def _sync_occurrences[
    OccORM: (TruePositiveOccurrenceORM, FalsePositiveOccurrenceORM),
    OccPydantic: (TruePositiveOccurrence, FalsePositiveOccurrence),
](
    session: Session,
    db_occs: list[OccORM],
    yaml_occs: list[OccPydantic],
    from_orm: Callable[[OccORM], OccPydantic],
    add_occ: Callable[[OccPydantic], None],
) -> bool:
    """Diff occurrences by id; delete+re-add only changed ones. Returns True if anything changed."""
    changed = False
    db_map = {o.occurrence_id: o for o in db_occs}
    yaml_map = {o.occurrence_id: o for o in yaml_occs}

    for occ_id in set(db_map) - set(yaml_map):
        session.delete(db_map[occ_id])
        changed = True

    for occ_id, yaml_occ in yaml_map.items():
        if occ_id not in db_map:
            add_occ(yaml_occ)
            changed = True
        elif from_orm(db_map[occ_id]) != yaml_occ:
            session.delete(db_map[occ_id])
            add_occ(yaml_occ)
            changed = True

    return changed


def _sync_tp_issue(session: Session, existing: TruePositive, yaml_issue: SyncTruePositive) -> bool:
    """Sync a TP issue, returning True if anything changed."""
    changed = existing.rationale != yaml_issue.rationale
    if changed:
        existing.rationale = yaml_issue.rationale

    def add(occ: TruePositiveOccurrence) -> None:
        _add_tp_occurrence(session, yaml_issue.snapshot_slug, yaml_issue.tp_id, occ)

    return _sync_occurrences(session, existing.occurrences, yaml_issue.occurrences, _tp_occ_from_orm, add) or changed


def _sync_fp_issue(session: Session, existing: FalsePositive, yaml_fp: SyncFalsePositive) -> bool:
    """Sync an FP issue, returning True if anything changed."""
    changed = existing.rationale != yaml_fp.rationale
    if changed:
        existing.rationale = yaml_fp.rationale

    def add(occ: FalsePositiveOccurrence) -> None:
        _add_fp_occurrence(session, yaml_fp.snapshot_slug, yaml_fp.fp_id, occ)

    return _sync_occurrences(session, existing.occurrences, yaml_fp.occurrences, _fp_occ_from_orm, add) or changed


def _add_tp_occurrence(session: Session, snapshot_slug: SnapshotSlug, tp_id: str, occ: TruePositiveOccurrence) -> None:
    """Create a TP occurrence ORM row with all dependent rows."""
    orm_occ = TruePositiveOccurrenceORM(
        snapshot_slug=snapshot_slug,
        tp_id=tp_id,
        occurrence_id=occ.occurrence_id,
        note=occ.note,
        match_file_restriction=ensure_file_set(session, snapshot_slug, occ.match_file_restriction),
    )
    session.add(orm_occ)
    _add_ranges_to_occurrence(orm_occ, occ.files)
    # Ranges will be cascade-saved when session flushes


def _add_fp_occurrence(session: Session, snapshot_slug: SnapshotSlug, fp_id: str, occ: FalsePositiveOccurrence) -> None:
    """Create an FP occurrence ORM row with all dependent rows."""
    orm_occ = FalsePositiveOccurrenceORM(
        snapshot_slug=snapshot_slug,
        fp_id=fp_id,
        occurrence_id=occ.occurrence_id,
        note=occ.note,
        match_file_restriction=ensure_file_set(session, snapshot_slug, occ.match_file_restriction),
    )
    session.add(orm_occ)
    _add_ranges_to_occurrence(orm_occ, occ.files)
    # Ranges will be cascade-saved when session flushes
    for relevant_file in occ.relevant_files:
        orm_occ.relevant_file_orms.append(
            FalsePositiveRelevantFileORM(
                snapshot_slug=snapshot_slug, fp_id=fp_id, occurrence_id=occ.occurrence_id, file_path=relevant_file
            )
        )


def compute_files_hash(file_paths: list[str]) -> str:
    """Compute content-addressable hash for a file set.

    Args:
        file_paths: List of relative file paths

    Returns:
        MD5 hash of sorted, newline-joined file paths
    """
    sorted_paths = sorted(file_paths)
    content = "\n".join(sorted_paths)
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def ensure_file_set(session: Session, snapshot_slug: SnapshotSlug, file_paths: AbstractSet[Path] | None) -> str | None:
    """Ensure a file_set exists for the given paths and return its hash.

    Upserts the FileSet and FileSetMember rows if they don't exist.
    Returns None if file_paths is None (pass-through for optional fields).

    Args:
        session: Database session
        snapshot_slug: Snapshot the file_set belongs to
        file_paths: Set of file paths, or None

    Returns:
        The files_hash for this file set, or None if file_paths is None
    """
    if file_paths is None:
        return None

    path_strs = [str(p) for p in file_paths]
    files_hash = compute_files_hash(path_strs)

    # Check if file_set already exists
    existing = session.query(FileSet).filter_by(snapshot_slug=snapshot_slug, files_hash=files_hash).first()
    if existing is None:
        # Create file_set and members
        fs = FileSet(snapshot_slug=snapshot_slug, files_hash=files_hash)
        session.add(fs)
        session.flush()
        for path_str in path_strs:
            session.add(FileSetMember(snapshot_slug=snapshot_slug, files_hash=files_hash, file_path=path_str))
        session.flush()

    return files_hash


def _sync_critic_scopes_for_specimen(
    session: Session,
    slug: SnapshotSlug,
    true_positives: list[SyncTruePositive],
    false_positives: list[SyncFalsePositive],
) -> None:
    """Sync file sets and critic_scopes_expected_to_recall for a specimen from parsed occurrence data.

    Note: File sets for match_file_restriction are already created by ensure_file_set during
    occurrence sync. This function only needs to handle critic_scopes_expected_to_recall.

    Args:
        session: Database session
        slug: Snapshot slug
        true_positives: List of parsed true positives with occurrences
        false_positives: List of parsed false positives with occurrences
    """
    logger.debug(f"Syncing critic scopes for {slug}: {len(true_positives)} TPs, {len(false_positives)} FPs")
    # Collect desired critic scopes from occurrence data
    desired_triggers: set[tuple[SnapshotSlug, str, str, str]] = set()

    for tp in true_positives:
        for occurrence in tp.occurrences:
            for trigger_files in occurrence.critic_scopes_expected_to_recall:
                # Ensure file set exists for this critic scope
                files_hash = ensure_file_set(session, slug, trigger_files)
                assert files_hash is not None  # trigger_files is not None, so hash shouldn't be None
                desired_triggers.add((slug, tp.tp_id, occurrence.occurrence_id, files_hash))

    logger.debug(f"Collected {len(desired_triggers)} desired critic scopes for {slug}")

    # Current critic scopes from DB
    existing_triggers: set[tuple[SnapshotSlug, str, str, str]] = {
        (t_slug, tp_id, occ_id, h)
        for t_slug, tp_id, occ_id, h in session.query(
            CriticScopeExpectedToRecall.snapshot_slug,
            CriticScopeExpectedToRecall.tp_id,
            CriticScopeExpectedToRecall.occurrence_id,
            CriticScopeExpectedToRecall.files_hash,
        )
        .filter_by(snapshot_slug=slug)
        .all()
    }

    # Diff critic scopes
    triggers_to_add = desired_triggers - existing_triggers
    triggers_to_delete = existing_triggers - desired_triggers

    for t_slug, tp_id, occurrence_id, files_hash in triggers_to_add:
        session.add(
            CriticScopeExpectedToRecall(
                snapshot_slug=t_slug, tp_id=tp_id, occurrence_id=occurrence_id, files_hash=files_hash
            )
        )

    if triggers_to_delete:
        session.query(CriticScopeExpectedToRecall).filter(
            tuple_(
                CriticScopeExpectedToRecall.snapshot_slug,
                CriticScopeExpectedToRecall.tp_id,
                CriticScopeExpectedToRecall.occurrence_id,
                CriticScopeExpectedToRecall.files_hash,
            ).in_(list(triggers_to_delete))
        ).delete(synchronize_session=False)

    session.flush()


def sync_specimen(session: Session, bundle: SpecimenBundle) -> None:
    """Sync a single specimen from bundle artifacts.

    Syncs the snapshot, snapshot files, and issues to the database within the
    provided session. Does not commit - caller is responsible for commit/rollback.

    Args:
        session: Database session (caller must commit/rollback)
        bundle: Specimen bundle with parsed data and code tar
    """
    # Use pre-parsed data from bundle
    specimen_data = bundle.data
    slug = bundle.slug

    # Read uncompressed tar (bundle and DB use same format)
    archive_bytes = bundle.code_tar.read_bytes()

    # Sync snapshot to DB
    snapshot_data = {"slug": slug, "split": specimen_data.split, "content": archive_bytes}

    stmt = insert(Snapshot).values(**snapshot_data).on_conflict_do_update(index_elements=["slug"], set_=snapshot_data)
    session.execute(stmt)
    session.flush()

    # Sync snapshot files using the bytes already in memory, bypassing the DB read.
    # The Core INSERT above does not update the ORM identity map, so re-reading snapshot.content
    # from the session would be unreliable for freshly upserted rows.
    sync_snapshot_files_to_db(session, slug, archive_bytes)

    # Convert issues dict to sync TruePositive/FalsePositive objects
    true_positives: list[SyncTruePositive] = []
    false_positives: list[SyncFalsePositive] = []

    for issue_id, issue in specimen_data.issues.items():
        # issue is already a YAMLIssue (validated by Pydantic when loading SpecimenData)
        if issue.should_flag:
            true_positives.append(issue.to_true_positive(tp_id=issue_id, snapshot_slug=slug))
        else:
            false_positives.append(issue.to_false_positive(fp_id=issue_id, snapshot_slug=slug))

    existing_issues = {
        (i.snapshot_slug, i.tp_id): i for i in session.query(TruePositive).filter_by(snapshot_slug=slug).all()
    }
    existing_fps = {
        (fp.snapshot_slug, fp.fp_id): fp for fp in session.query(FalsePositive).filter_by(snapshot_slug=slug).all()
    }

    seen_issue_keys: set[tuple[SnapshotSlug, str]] = set()
    seen_fp_keys: set[tuple[SnapshotSlug, str]] = set()

    # Sync true positives
    for tp in true_positives:
        key = (tp.snapshot_slug, tp.tp_id)
        seen_issue_keys.add(key)

        if key not in existing_issues:
            logger.debug(f"Adding issue: {tp.snapshot_slug}/{tp.tp_id}")
            orm_issue = TruePositive(snapshot_slug=tp.snapshot_slug, tp_id=tp.tp_id, rationale=tp.rationale)
            session.add(orm_issue)
            for occ in tp.occurrences:
                _add_tp_occurrence(session, tp.snapshot_slug, tp.tp_id, occ)
        else:
            existing = existing_issues[key]
            _sync_tp_issue(session, existing, tp)

    # Sync false positives
    for fp in false_positives:
        fp_key = (fp.snapshot_slug, fp.fp_id)
        seen_fp_keys.add(fp_key)

        if fp_key not in existing_fps:
            logger.debug(f"Adding false positive: {fp.snapshot_slug}/{fp.fp_id}")
            orm_fp = FalsePositive(snapshot_slug=fp.snapshot_slug, fp_id=fp.fp_id, rationale=fp.rationale)
            session.add(orm_fp)
            for fp_occ in fp.occurrences:
                _add_fp_occurrence(session, fp.snapshot_slug, fp.fp_id, fp_occ)
        else:
            existing_fp = existing_fps[fp_key]
            _sync_fp_issue(session, existing_fp, fp)

    # Delete orphaned issues (in DB but not in bundle) in bulk.
    if orphaned_tp_keys := existing_issues.keys() - seen_issue_keys:
        session.query(TruePositive).filter(
            tuple_(TruePositive.snapshot_slug, TruePositive.tp_id).in_(list(orphaned_tp_keys))
        ).delete(synchronize_session=False)
    if orphaned_fp_keys := existing_fps.keys() - seen_fp_keys:
        session.query(FalsePositive).filter(
            tuple_(FalsePositive.snapshot_slug, FalsePositive.fp_id).in_(list(orphaned_fp_keys))
        ).delete(synchronize_session=False)

    session.flush()

    _sync_critic_scopes_for_specimen(session, slug, true_positives, false_positives)

    logger.info(f"Synced specimen from bundle: {slug}")
