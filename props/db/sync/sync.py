"""Sync snapshots, issues, and model metadata from filesystem to database."""

from __future__ import annotations

import hashlib
import io
import logging
import shutil
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlunparse
from urllib.request import urlopen

import pygit2
from opentelemetry import trace
from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from props.core.ids import SnapshotSlug
from props.core.models.snapshot import BundleFilter, GitHubSource, GitSource, LocalSource, SnapshotDoc
from props.core.models.true_positive import FalsePositiveOccurrence, LineRange, TruePositiveOccurrence
from props.core.runs_context import specimens_definitions_root
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
from props.db.sync.loader import discover_snapshots
from props.db.sync.model_metadata import sync_model_metadata_with_session
from props.db.sync.stats import SyncStats
from props.db.sync.yaml_loader import (
    FalsePositive as FalsePositive_yaml,
    SyncValidationError,
    TruePositive as TruePositive_yaml,
    load_yaml_issues,
)

if TYPE_CHECKING:
    from props.config import PropsConfig

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


def _add_ranges_to_occurrence(
    orm_occ: TruePositiveOccurrenceORM | FalsePositiveOccurrenceORM, files: dict[Path, list[LineRange] | None]
) -> None:
    """Add OccurrenceRangeORM objects to an ORM occurrence from a files dict."""
    for file_path, ranges in files.items():
        if ranges is not None:
            for range_id, line_range in enumerate(ranges):
                orm_occ.ranges.append(
                    OccurrenceRangeORM(
                        file_path=file_path,
                        range_id=range_id,
                        start_line=line_range.start_line,
                        end_line=line_range.end_line if line_range.end_line is not None else line_range.start_line,
                        note=line_range.note,
                    )
                )


def get_specimens_base_path() -> Path:
    """Get specimens base path from ADGN_PROPS_SPECIMENS_ROOT environment variable.

    Returns:
        Path to specimens directory

    Raises:
        ValueError: If ADGN_PROPS_SPECIMENS_ROOT environment variable not set
        FileNotFoundError: If specimens directory doesn't exist or missing required files
    """
    return specimens_definitions_root()


def _download_github_tarball_to_temp(owner: str, repo: str, ref: str) -> Path:
    """Download GitHub tarball to temp directory, return extracted content root."""
    url = urlunparse(("https", "codeload.github.com", f"/{owner}/{repo}/tar.gz/{ref}", "", "", ""))
    logger.debug("downloading %s", url)
    try:
        with urlopen(url) as resp:
            tarball_bytes = resp.read()
    except (URLError, HTTPError) as e:
        raise RuntimeError(f"GitHub download failed: {e}") from e

    # Extract directly from bytes
    tmpdir = Path(tempfile.mkdtemp(prefix="adgn-sync-"))
    with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tf:
        tf.extractall(tmpdir, filter=_safe_tar_filter)

    # GitHub tarballs have top-level dir like "repo-commit/", return that
    for p in tmpdir.iterdir():
        if p.is_dir():
            return p
    return tmpdir


def _clone_git_to_temp(url: str, ref: str) -> Path:
    """Clone git repo to temp directory, return content root."""
    tmpdir = Path(tempfile.mkdtemp(prefix="adgn-sync-"))
    try:
        repo = pygit2.clone_repository(url, str(tmpdir), bare=False)
        commit = repo.revparse_single(ref)
        repo.checkout_tree(commit)
        repo.set_head(commit.id)
        shutil.rmtree(tmpdir / ".git", ignore_errors=True)
        return tmpdir
    except (pygit2.GitError, KeyError) as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError(f"Git clone failed for {url}@{ref}: {e}") from e


def _safe_tar_filter(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
    """Tarfile filter that skips absolute symlinks."""
    try:
        return tarfile.data_filter(member, path)
    except tarfile.AbsoluteLinkError:
        logger.warning(f"Skipping absolute symlink: {member.name} -> {member.linkname}")
        return None


def resolve_git_content(manifest: SnapshotDoc, slug: SnapshotSlug) -> Path:
    """Resolve GitSource/GitHubSource to local temp directory.

    Returns path to directory containing source code (caller must clean up after use).
    No file-based caching - DB stores the final archives.
    """
    source = manifest.source

    if isinstance(source, GitHubSource):
        return _download_github_tarball_to_temp(source.org, source.repo, source.ref)

    if isinstance(source, GitSource):
        # Try GitHub fast path for github.com URLs
        if source.url.startswith("https://github.com/"):
            parts = source.url.removeprefix("https://github.com/").rstrip("/").removesuffix(".git").split("/")
            if len(parts) >= 2:
                try:
                    return _download_github_tarball_to_temp(parts[0], parts[1], source.commit)
                except RuntimeError:
                    pass  # Fall through to git clone
        # Fall back to git clone
        return _clone_git_to_temp(source.url, source.commit)

    raise ValueError(f"resolve_git_content called with non-git source: {type(source)}")


def _matches_bundle_pattern(path: str, pattern: str) -> bool:
    """Match path against gitignore-style pattern.

    - Trailing slash means directory prefix (e.g., "web/" matches "web/foo.py")
    - No trailing slash matches as prefix or exact (e.g., "foo" matches "foo.py" and "foo/bar.py")
    """
    if pattern.endswith("/"):
        # Directory pattern: matches if path starts with pattern (without trailing /)
        return path.startswith(pattern[:-1] + "/") or path == pattern[:-1]
    # Prefix or exact match
    return path.startswith(pattern) or path == pattern


def create_snapshot_archive(content_dir: Path, bundle_filter: BundleFilter | None = None) -> bytes:
    """Create uncompressed tar archive from directory.

    Args:
        content_dir: Directory containing source files to archive
        bundle_filter: Optional filter with include/exclude patterns

    Returns:
        Uncompressed tar archive as bytes
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path in sorted(content_dir.rglob("*")):  # sorted for determinism
            if not path.is_file():
                continue

            rel_path = path.relative_to(content_dir)
            rel_str = str(rel_path)

            # Skip VCS internals
            if ".git" in rel_path.parts:
                continue

            # Apply bundle filter if present
            if bundle_filter:
                # Check include patterns (if specified, file must match at least one)
                if bundle_filter.include and not any(
                    _matches_bundle_pattern(rel_str, p) for p in bundle_filter.include
                ):
                    continue

                # Check exclude patterns (if matches any, skip)
                if bundle_filter.exclude and any(_matches_bundle_pattern(rel_str, p) for p in bundle_filter.exclude):
                    continue

            # TODO: Symlink support was removed because Bazel runfiles are symlinks,
            # causing tar to store them as symlink entries which break snapshot_files
            # extraction. To restore relative symlink support: check if the resolved
            # target is under content_dir — if so, keep as symlink; otherwise dereference.
            source_path = str(path)
            if path.is_symlink():
                logger.warning(f"Dereferencing symlink in snapshot archive: {rel_str}")
                source_path = str(path.resolve())

            # Add file to archive with deterministic mtime
            info = tar.gettarinfo(source_path, arcname=rel_str)
            info.mtime = 0  # Deterministic for reproducibility
            with path.open("rb") as f:
                tar.addfile(info, f)

    return buffer.getvalue()


def sync_snapshots_to_db(
    session: Session, snapshots: dict[SnapshotSlug, SnapshotDoc], specimens_dir: Path
) -> SyncStats:
    """Sync snapshots to database, creating content archives from specimens repo."""

    # Get existing snapshots from DB
    existing = {s.slug: s for s in session.query(Snapshot).all()}
    source_slugs = set(snapshots.keys())
    db_slugs = set(existing.keys())

    # Track stats
    added = 0
    updated = 0
    deleted = 0

    # Delete orphaned snapshots (in DB but not in source)
    for slug in db_slugs - source_slugs:
        logger.info(f"Deleting orphaned snapshot: {slug}")
        session.delete(existing[slug])
        deleted += 1

    # Add/update snapshots from source
    total_snapshots = len(snapshots)
    total_archive_bytes = 0
    for idx, (slug, manifest) in enumerate(snapshots.items(), 1):
        # Resolve content directory based on source type
        cleanup_dir: Path | None = None
        if isinstance(manifest.source, LocalSource):
            content_dir = (specimens_dir / slug / manifest.source.root).resolve()
        elif isinstance(manifest.source, GitSource | GitHubSource):
            content_dir = resolve_git_content(manifest, slug)
            cleanup_dir = content_dir.parent if content_dir.parent.name.startswith("adgn-sync-") else content_dir
        else:
            raise ValueError(f"Unknown source type for {slug}: {type(manifest.source)}")

        try:
            # Create tar archive from content directory
            archive = create_snapshot_archive(content_dir, manifest.bundle)
        finally:
            # Clean up temp directory for git sources
            if cleanup_dir is not None:
                shutil.rmtree(cleanup_dir, ignore_errors=True)

        archive_size = len(archive)
        total_archive_bytes += archive_size
        print(f"  [{idx}/{total_snapshots}] {slug} ({archive_size / 1024:.1f} KB)")

        # Convert Pydantic model to dict for upsert
        snapshot_data = {
            "slug": slug,
            "split": manifest.split,
            "content": archive,
            "source": manifest.source.model_dump(mode="json") if manifest.source else None,
            "bundle": manifest.bundle.model_dump(mode="json") if manifest.bundle else None,
        }

        if slug not in db_slugs:
            # New snapshot - insert
            logger.debug(f"Adding snapshot: {slug} (split={manifest.split}, size={archive_size} bytes)")
            stmt = insert(Snapshot).values(**snapshot_data)
            session.execute(stmt)
            added += 1
        else:
            # Always update content (content comparison would be expensive)
            logger.debug(f"Updating snapshot: {slug} (size={archive_size} bytes)")
            stmt = (
                insert(Snapshot)
                .values(**snapshot_data)
                .on_conflict_do_update(index_elements=["slug"], set_=snapshot_data)
            )
            session.execute(stmt)
            updated += 1

    session.flush()
    total = len(snapshots)
    print(f"  Total archive size: {total_archive_bytes / 1024 / 1024:.1f} MB")
    logger.info(f"Snapshots synced: +{added} added, ~{updated} updated, -{deleted} deleted, ={total} total")
    return SyncStats(total=total, added=added, updated=updated, deleted=deleted)


def sync_issues_to_db(
    session: Session, slugs: list[SnapshotSlug], specimens_dir: Path, *, collect_errors: bool = False
) -> SyncStats:
    """Sync issues and false positives from filesystem to database.

    When collect_errors is True, per-snapshot errors are collected using
    savepoints. Raises SyncValidationError at the end if any snapshots failed.
    """

    # Track stats across both TPs and FPs
    total = 0
    added = 0
    updated = 0
    deleted = 0

    errors: list[str] = []
    failed_slugs: set[SnapshotSlug] = set()

    # Get existing issues and FPs from DB
    existing_issues = {(i.snapshot_slug, i.tp_id): i for i in session.query(TruePositive).all()}
    existing_fps = {(fp.snapshot_slug, fp.fp_id): fp for fp in session.query(FalsePositive).all()}

    # Track which issues/FPs we've seen (to detect deletions)
    seen_issue_keys: set[tuple[SnapshotSlug, str]] = set()
    seen_fp_keys: set[tuple[SnapshotSlug, str]] = set()

    # Process each snapshot
    for slug in slugs:
        savepoint = session.begin_nested()
        try:
            true_positives, false_positives = load_yaml_issues(slug, specimens_dir, collect_errors=collect_errors)

            # Sync true positives
            for issue in true_positives:
                key = (issue.snapshot_slug, issue.tp_id)
                seen_issue_keys.add(key)

                if key not in existing_issues:
                    logger.debug(f"Adding issue: {issue.snapshot_slug}/{issue.tp_id}")
                    orm_issue = TruePositive(
                        snapshot_slug=issue.snapshot_slug, tp_id=issue.tp_id, rationale=issue.rationale
                    )
                    session.add(orm_issue)
                    for occ in issue.occurrences:
                        _add_tp_occurrence(session, issue.snapshot_slug, issue.tp_id, occ)
                    added += 1
                    total += 1
                else:
                    existing = existing_issues[key]
                    if _sync_tp_issue(session, existing, issue):
                        updated += 1
                    total += 1

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
                    added += 1
                    total += 1
                else:
                    existing_fp = existing_fps[fp_key]
                    if _sync_fp_issue(session, existing_fp, fp):
                        updated += 1
                    total += 1

            session.flush()
        except Exception as e:
            savepoint.rollback()
            if not collect_errors:
                raise
            if isinstance(e, SyncValidationError):
                errors.extend(e.errors)
            else:
                errors.append(f"{slug}: {e}")
            failed_slugs.add(slug)
        else:
            savepoint.commit()

    # Delete orphaned issues (in DB but not in source, skip failed slugs)
    for key in set(existing_issues.keys()) - seen_issue_keys:
        if key[0] in failed_slugs:
            continue
        logger.info(f"Deleting orphaned issue: {key[0]}/{key[1]}")
        session.delete(existing_issues[key])
        deleted += 1

    # Delete orphaned FPs (in DB but not in source, skip failed slugs)
    for key in set(existing_fps.keys()) - seen_fp_keys:
        if key[0] in failed_slugs:
            continue
        logger.info(f"Deleting orphaned false positive: {key[0]}/{key[1]}")
        session.delete(existing_fps[key])
        deleted += 1

    session.flush()
    logger.info(f"Issues synced: +{added} added, ~{updated} updated, -{deleted} deleted, ={total} total")

    if errors:
        raise SyncValidationError(errors, failed_slugs=failed_slugs)

    return SyncStats(total=total, added=added, updated=updated, deleted=deleted)


def sync_snapshot_files_to_db(session: Session, slugs: list[SnapshotSlug]) -> SyncStats:
    """Sync snapshot_files table from snapshot content archives in DB."""

    # Get existing files from DB (primitive tuples to avoid detached ORM access)
    existing_keys: set[tuple[SnapshotSlug, str]] = {
        (row[0], row[1]) for row in session.execute(select(SnapshotFile.snapshot_slug, SnapshotFile.file_path))
    }
    seen_keys: set[tuple[SnapshotSlug, str]] = set()

    total = 0
    added = 0
    updated = 0
    deleted = 0

    for slug in slugs:
        # Get content from database
        snapshot = session.query(Snapshot).filter_by(slug=slug).one()
        if snapshot.content is None:
            logger.warning(f"Snapshot {slug} has no content, skipping file sync")
            continue

        # Extract file list from tar archive
        buffer = io.BytesIO(snapshot.content)
        with tarfile.open(fileobj=buffer, mode="r") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue

                relative = member.name
                key = (slug, relative)
                seen_keys.add(key)

                # Read file content to count lines
                f = tar.extractfile(member)
                if f is None:
                    continue
                content = f.read().decode("utf-8", errors="replace")
                line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)

                # Upsert line_count via ON CONFLICT to avoid ORM instance usage
                stmt = (
                    insert(SnapshotFile)
                    .values(snapshot_slug=slug, file_path=relative, line_count=line_count)
                    .on_conflict_do_update(
                        index_elements=[SnapshotFile.snapshot_slug, SnapshotFile.file_path],
                        set_={"line_count": line_count},
                    )
                )
                session.execute(stmt)
                if key in existing_keys:
                    updated += 1
                else:
                    added += 1

                total += 1

    # Delete orphaned files
    for snapshot_slug, file_path in existing_keys - seen_keys:
        session.query(SnapshotFile).filter_by(snapshot_slug=snapshot_slug, file_path=file_path).delete()
        deleted += 1

    session.flush()
    logger.info(f"Snapshot files synced: +{added} added, ~{updated} updated, -{deleted} deleted, ={total} total")
    return SyncStats(total=total, added=added, updated=updated, deleted=deleted)


_OccORM = TypeVar("_OccORM", TruePositiveOccurrenceORM, FalsePositiveOccurrenceORM)
_OccPydantic = TypeVar("_OccPydantic", TruePositiveOccurrence, FalsePositiveOccurrence)


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


def _sync_occurrences(
    session: Session,
    db_occs: list[_OccORM],
    yaml_occs: list[_OccPydantic],
    from_orm: Callable[[_OccORM], _OccPydantic],
    add_occ: Callable[[_OccPydantic], None],
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


def _sync_tp_issue(session: Session, existing: TruePositive, yaml_issue: TruePositive_yaml) -> bool:
    """Sync a TP issue, returning True if anything changed."""
    changed = existing.rationale != yaml_issue.rationale
    if changed:
        existing.rationale = yaml_issue.rationale

    def add(occ: TruePositiveOccurrence) -> None:
        _add_tp_occurrence(session, yaml_issue.snapshot_slug, yaml_issue.tp_id, occ)

    return _sync_occurrences(session, existing.occurrences, yaml_issue.occurrences, _tp_occ_from_orm, add) or changed


def _sync_fp_issue(session: Session, existing: FalsePositive, yaml_fp: FalsePositive_yaml) -> bool:
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
    for relevant_file in occ.relevant_files:
        orm_occ.relevant_file_orms.append(
            FalsePositiveRelevantFileORM(
                snapshot_slug=snapshot_slug, occurrence_id=occ.occurrence_id, file_path=relevant_file
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


def ensure_file_set(session: Session, snapshot_slug: SnapshotSlug, file_paths: set[Path] | None) -> str | None:
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
        session.flush()  # Ensure FK for members
        for path_str in path_strs:
            session.add(FileSetMember(snapshot_slug=snapshot_slug, files_hash=files_hash, file_path=path_str))

    return files_hash


def sync_file_sets_to_db(session: Session, slugs: list[SnapshotSlug], specimens_dir: Path) -> SyncStats:
    """Sync file_sets, file_set_members, and critic_scopes_expected_to_recall from YAML sources.

    Reads critic_scopes_expected_to_recall from YAML files (the source of truth), not from ORM.
    """
    desired_file_sets: dict[tuple[SnapshotSlug, str], list[str]] = {}
    desired_triggers: set[tuple[SnapshotSlug, str, str, str]] = set()

    # Load canonical TPs from YAML to get critic_scopes_expected_to_recall

    for slug in slugs:
        true_positives, false_positives = load_yaml_issues(slug, specimens_dir)

        for tp in true_positives:
            for occurrence in tp.occurrences:
                for trigger_files in occurrence.critic_scopes_expected_to_recall:
                    # Convert to strings and compute hash
                    file_paths = [str(f) for f in trigger_files]
                    files_hash = compute_files_hash(file_paths)

                    key = (slug, files_hash)
                    desired_file_sets.setdefault(key, file_paths)
                    desired_triggers.add((slug, tp.tp_id, occurrence.occurrence_id, files_hash))

                # Also preserve file sets used by match_file_restriction
                if occurrence.match_file_restriction is not None:
                    restriction_paths = [str(f) for f in occurrence.match_file_restriction]
                    restriction_hash = compute_files_hash(restriction_paths)
                    desired_file_sets.setdefault((slug, restriction_hash), restriction_paths)

        for fp in false_positives:
            for fp_occ in fp.occurrences:
                if fp_occ.match_file_restriction is not None:
                    restriction_paths = [str(f) for f in fp_occ.match_file_restriction]
                    restriction_hash = compute_files_hash(restriction_paths)
                    desired_file_sets.setdefault((slug, restriction_hash), restriction_paths)

    # Current state from DB
    existing_file_sets: set[tuple[SnapshotSlug, str]] = {
        (slug, h) for slug, h in session.query(FileSet.snapshot_slug, FileSet.files_hash).all()
    }
    existing_triggers: set[tuple[SnapshotSlug, str, str, str]] = {
        (slug, tp_id, occ_id, h)
        for slug, tp_id, occ_id, h in session.query(
            CriticScopeExpectedToRecall.snapshot_slug,
            CriticScopeExpectedToRecall.tp_id,
            CriticScopeExpectedToRecall.occurrence_id,
            CriticScopeExpectedToRecall.files_hash,
        ).all()
    }

    # Diff file sets
    to_add = desired_file_sets.keys() - existing_file_sets
    to_delete = existing_file_sets - desired_file_sets.keys()

    file_sets_added = 0
    file_sets_deleted = 0

    for slug, files_hash in to_add:
        file_paths = desired_file_sets[(slug, files_hash)]
        fs = FileSet(snapshot_slug=slug, files_hash=files_hash)
        session.add(fs)
        session.flush()  # ensure FK for members
        for file_path in file_paths:
            session.add(FileSetMember(snapshot_slug=slug, files_hash=files_hash, file_path=file_path))
        file_sets_added += 1

    for slug, files_hash in to_delete:
        # Clear match_file_restriction on occurrences before deleting file_set (FK RESTRICT)
        session.query(TruePositiveOccurrenceORM).filter_by(
            snapshot_slug=slug, match_file_restriction=files_hash
        ).update({TruePositiveOccurrenceORM.match_file_restriction: None})
        session.query(FalsePositiveOccurrenceORM).filter_by(
            snapshot_slug=slug, match_file_restriction=files_hash
        ).update({FalsePositiveOccurrenceORM.match_file_restriction: None})
        session.query(FileSet).filter_by(snapshot_slug=slug, files_hash=files_hash).delete()
        file_sets_deleted += 1

    # Diff occurrence triggers
    triggers_to_add = desired_triggers - existing_triggers
    triggers_to_delete = existing_triggers - desired_triggers

    critic_scopes_expected_to_recall_added = 0
    critic_scopes_expected_to_recall_deleted = 0

    for slug, tp_id, occurrence_id, files_hash in triggers_to_add:
        session.add(
            CriticScopeExpectedToRecall(
                snapshot_slug=slug, tp_id=tp_id, occurrence_id=occurrence_id, files_hash=files_hash
            )
        )
        critic_scopes_expected_to_recall_added += 1

    if triggers_to_delete:
        session.query(CriticScopeExpectedToRecall).filter(
            tuple_(
                CriticScopeExpectedToRecall.snapshot_slug,
                CriticScopeExpectedToRecall.tp_id,
                CriticScopeExpectedToRecall.occurrence_id,
                CriticScopeExpectedToRecall.files_hash,
            ).in_(list(triggers_to_delete))
        ).delete(synchronize_session=False)
        critic_scopes_expected_to_recall_deleted = len(triggers_to_delete)

    session.flush()
    logger.info(
        "File sets synced: +%d added, -%d deleted; critic_scopes_expected_to_recall +%d, -%d",
        file_sets_added,
        file_sets_deleted,
        critic_scopes_expected_to_recall_added,
        critic_scopes_expected_to_recall_deleted,
    )
    total = len(desired_file_sets)
    return SyncStats(total=total, added=file_sets_added, updated=0, deleted=file_sets_deleted)


@dataclass
class FullSyncResult:
    """Combined result from syncing snapshots, issues, files, file sets, and model metadata."""

    snapshot_stats: SyncStats
    issue_stats: SyncStats
    snapshot_file_stats: SyncStats
    file_set_stats: SyncStats
    model_metadata_stats: SyncStats


def sync_all(
    session: Session,
    *,
    config: PropsConfig | None = None,
    use_staged: bool = False,
    dry_run: bool = False,
    collect_errors: bool = False,
) -> FullSyncResult:
    """Sync snapshots, issues, files, file sets, and model metadata.

    Discovers snapshots once and passes data to all sync operations.
    All sync operations happen within the provided database session for consistency.

    Sync order is critical:
    1. snapshots (creates content archives from specimens repo)
    2. snapshot_files (reads from DB content column)
    3. issues (depends on snapshots)
    4. file_sets (depends on snapshot_files and issues via FK)
    5. model_metadata (independent, but needs config for custom models)

    When collect_errors is True, stages 3-4 collect per-snapshot errors instead
    of failing on the first one. The entire transaction is rolled back on any failure.
    """
    with tracer.start_as_current_span("sync_all"):
        specimens_dir = get_specimens_base_path()

        # Discover snapshots once
        print(f"Discovering snapshots from {specimens_dir}...")
        snapshots = discover_snapshots(specimens_dir)
        slugs = list(snapshots.keys())
        print(f"  Found {len(snapshots)} snapshots")

        # 1. Sync snapshots (creates content archives from filesystem)
        print("Syncing snapshots (creating tar archives)...")
        snapshot_stats = sync_snapshots_to_db(session, snapshots, specimens_dir)
        print(f"  {snapshot_stats.summary_text}")

        # 2. Sync snapshot files (reads from DB content column)
        print("Syncing snapshot files...")
        snapshot_file_stats = sync_snapshot_files_to_db(session, slugs)
        print(f"  {snapshot_file_stats.summary_text}")

        # 3. Sync issues (collect errors per-snapshot when enabled)
        all_errors: list[str] = []
        failed_slugs: set[SnapshotSlug] = set()

        print("Syncing issues...")
        try:
            issue_stats = sync_issues_to_db(session, slugs, specimens_dir, collect_errors=collect_errors)
        except SyncValidationError as e:
            all_errors.extend(e.errors)
            failed_slugs.update(e.failed_slugs)
            issue_stats = SyncStats(total=0, added=0, updated=0, deleted=0)
        print(f"  {issue_stats.summary_text}")

        # 4. Sync file sets (skip failed slugs from stage 3)
        remaining_slugs = [s for s in slugs if s not in failed_slugs]
        print("Syncing file sets...")
        file_set_stats = sync_file_sets_to_db(session, remaining_slugs, specimens_dir)
        print(f"  {file_set_stats.summary_text}")

        # 5. Sync model metadata
        print("Syncing model metadata...")
        model_metadata_stats = sync_model_metadata_with_session(session, config)
        print(f"  {model_metadata_stats.summary_text}")

        # Final commit/rollback
        if all_errors:
            session.rollback()
            raise SyncValidationError(all_errors, failed_slugs=failed_slugs)
        if dry_run:
            logger.info("DRY-RUN: Rolling back all changes")
            session.rollback()
        else:
            session.commit()

        return FullSyncResult(
            snapshot_stats=snapshot_stats,
            issue_stats=issue_stats,
            snapshot_file_stats=snapshot_file_stats,
            file_set_stats=file_set_stats,
            model_metadata_stats=model_metadata_stats,
        )
