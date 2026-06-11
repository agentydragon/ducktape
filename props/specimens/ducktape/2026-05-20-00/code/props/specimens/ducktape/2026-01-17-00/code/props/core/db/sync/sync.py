"""Sync snapshots, issues, and model metadata from filesystem to database.

Replaces sync_specimens.py with new snapshot-based schema.
Includes model metadata sync (previously in sync_model_metadata.py).
"""

from __future__ import annotations

import hashlib
import io
import logging
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.parse import urlunparse
from urllib.request import urlopen

import pygit2
from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from openai_utils.model_metadata import MODEL_METADATA
from props.core.db.models import (
    CriticScopeExpectedToRecall,
    FalsePositive,
    FalsePositiveOccurrenceORM,
    FalsePositiveRelevantFileORM,
    FileSet,
    FileSetMember,
    ModelMetadata,
    OccurrenceRangeORM,
    Snapshot,
    SnapshotFile,
    TruePositive,
    TruePositiveOccurrenceORM,
)
from props.core.db.session import get_session
from props.core.ids import SnapshotSlug
from props.core.models.snapshot import BundleFilter, GitHubSource, GitSource, LocalSource, SnapshotDoc
from props.core.runs_context import specimens_definitions_root

from ._yaml import load_yaml_issues
from .loader import discover_snapshots

if TYPE_CHECKING:
    from props.core.models.true_positive import LineRange

# Agent definitions are stored in the props package under agent_defs/
AGENT_DEFS_PATH = Path(__file__).parent.parent.parent / "agent_defs"

logger = logging.getLogger(__name__)


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

            # Add file to archive with deterministic mtime
            info = tar.gettarinfo(str(path), arcname=rel_str)
            info.mtime = 0  # Deterministic for reproducibility
            with path.open("rb") as f:
                tar.addfile(info, f)

    return buffer.getvalue()


@dataclass
class SyncStats:
    """Statistics from a sync operation."""

    total: int
    added: int
    updated: int
    deleted: int

    @property
    def summary_text(self) -> str:
        """Format as human-readable summary."""
        return f"{self.total} total (+{self.added}, ~{self.updated}, -{self.deleted})"


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

    session.commit()
    total = len(snapshots)
    print(f"  Total archive size: {total_archive_bytes / 1024 / 1024:.1f} MB")
    logger.info(f"Snapshots synced: +{added} added, ~{updated} updated, -{deleted} deleted, ={total} total")
    return SyncStats(total=total, added=added, updated=updated, deleted=deleted)


def sync_issues_to_db(session: Session, slugs: list[SnapshotSlug], specimens_dir: Path) -> SyncStats:
    """Sync issues and false positives from filesystem to database."""

    # Track stats across both TPs and FPs
    total = 0
    added = 0
    updated = 0
    deleted = 0

    # Get existing issues and FPs from DB

    existing_issues = {(i.snapshot_slug, i.tp_id): i for i in session.query(TruePositive).all()}
    existing_fps = {(fp.snapshot_slug, fp.fp_id): fp for fp in session.query(FalsePositive).all()}

    # Track which issues/FPs we've seen (to detect deletions)
    seen_issue_keys = set()
    seen_fp_keys = set()

    # Process each snapshot
    for slug in slugs:
        true_positives, false_positives = load_yaml_issues(slug, specimens_dir)

        # Sync true positives
        for issue in true_positives:
            key = (issue.snapshot_slug, issue.tp_id)
            seen_issue_keys.add(key)

            if key not in existing_issues:
                # New issue - create ORM instance and occurrences
                logger.debug(f"Adding issue: {issue.snapshot_slug}/{issue.tp_id}")
                orm_issue = TruePositive(
                    snapshot_slug=issue.snapshot_slug, tp_id=issue.tp_id, rationale=issue.rationale
                )
                session.add(orm_issue)
                # Add occurrences to normalized table
                # Note: critic_scopes_expected_to_recall is stored in critic_scopes_expected_to_recall M:N table, not as column
                # Note: files/line ranges are stored in tp_occurrence_ranges table, not as column
                for occ in issue.occurrences:
                    orm_occ = TruePositiveOccurrenceORM(
                        snapshot_slug=issue.snapshot_slug,
                        tp_id=issue.tp_id,
                        occurrence_id=occ.occurrence_id,
                        note=occ.note,
                        graders_match_only_if_reported_on=ensure_file_set(
                            session, issue.snapshot_slug, occ.graders_match_only_if_reported_on
                        ),
                    )
                    session.add(orm_occ)
                    _add_ranges_to_occurrence(orm_occ, occ.files)
                added += 1
                total += 1
            else:
                # Existing issue - check if update needed
                existing = existing_issues[key]
                needs_update = False

                if existing.rationale != issue.rationale:
                    logger.debug(f"Updating issue rationale: {key}")
                    needs_update = True

                # For now, always update occurrences if any change detected
                # TODO: Implement proper occurrence comparison
                if needs_update:
                    existing.rationale = issue.rationale
                    # Delete existing occurrences and re-add (cascade handles this)
                    for occ_orm in list(existing.occurrences):
                        session.delete(occ_orm)
                    for occ in issue.occurrences:
                        orm_occ = TruePositiveOccurrenceORM(
                            snapshot_slug=issue.snapshot_slug,
                            tp_id=issue.tp_id,
                            occurrence_id=occ.occurrence_id,
                            note=occ.note,
                            graders_match_only_if_reported_on=ensure_file_set(
                                session, issue.snapshot_slug, occ.graders_match_only_if_reported_on
                            ),
                        )
                        session.add(orm_occ)
                        _add_ranges_to_occurrence(orm_occ, occ.files)
                    updated += 1
                    total += 1
                else:
                    total += 1

        # Sync false positives
        for fp in false_positives:
            fp_key = (fp.snapshot_slug, fp.fp_id)
            seen_fp_keys.add(fp_key)

            if fp_key not in existing_fps:
                # New FP - create ORM instance and occurrences
                logger.debug(f"Adding false positive: {fp.snapshot_slug}/{fp.fp_id}")
                orm_fp = FalsePositive(snapshot_slug=fp.snapshot_slug, fp_id=fp.fp_id, rationale=fp.rationale)
                session.add(orm_fp)
                for fp_occ in fp.occurrences:
                    fp_orm_occ = FalsePositiveOccurrenceORM(
                        snapshot_slug=fp.snapshot_slug,
                        fp_id=fp.fp_id,
                        occurrence_id=fp_occ.occurrence_id,
                        note=fp_occ.note,
                        graders_match_only_if_reported_on=ensure_file_set(
                            session, fp.snapshot_slug, fp_occ.graders_match_only_if_reported_on
                        ),
                    )
                    session.add(fp_orm_occ)
                    _add_ranges_to_occurrence(fp_orm_occ, fp_occ.files)
                    for relevant_file in fp_occ.relevant_files:
                        fp_orm_occ.relevant_file_orms.append(
                            FalsePositiveRelevantFileORM(
                                snapshot_slug=fp.snapshot_slug,
                                occurrence_id=fp_occ.occurrence_id,
                                file_path=relevant_file,
                            )
                        )
                added += 1
                total += 1
            else:
                # Existing FP - check if update needed
                existing_fp = existing_fps[fp_key]
                fp_needs_update = False

                if existing_fp.rationale != fp.rationale:
                    logger.debug(f"Updating FP rationale: {fp_key}")
                    fp_needs_update = True

                # For now, always update occurrences if any change detected
                # TODO: Implement proper occurrence comparison
                if fp_needs_update:
                    existing_fp.rationale = fp.rationale
                    # Delete existing occurrences and re-add (cascade handles this)
                    for fp_occ_orm in list(existing_fp.occurrences):
                        session.delete(fp_occ_orm)
                    for fp_occ in fp.occurrences:
                        fp_orm_occ = FalsePositiveOccurrenceORM(
                            snapshot_slug=fp.snapshot_slug,
                            fp_id=fp.fp_id,
                            occurrence_id=fp_occ.occurrence_id,
                            note=fp_occ.note,
                            graders_match_only_if_reported_on=ensure_file_set(
                                session, fp.snapshot_slug, fp_occ.graders_match_only_if_reported_on
                            ),
                        )
                        session.add(fp_orm_occ)
                        _add_ranges_to_occurrence(fp_orm_occ, fp_occ.files)
                        for relevant_file in fp_occ.relevant_files:
                            fp_orm_occ.relevant_file_orms.append(
                                FalsePositiveRelevantFileORM(
                                    snapshot_slug=fp.snapshot_slug,
                                    occurrence_id=fp_occ.occurrence_id,
                                    file_path=relevant_file,
                                )
                            )
                    updated += 1
                    total += 1
                else:
                    total += 1

    # Delete orphaned issues (in DB but not in source)
    for key in set(existing_issues.keys()) - seen_issue_keys:
        logger.info(f"Deleting orphaned issue: {key[0]}/{key[1]}")
        session.delete(existing_issues[key])
        deleted += 1

    # Delete orphaned FPs (in DB but not in source)
    for key in set(existing_fps.keys()) - seen_fp_keys:
        logger.info(f"Deleting orphaned false positive: {key[0]}/{key[1]}")
        session.delete(existing_fps[key])
        deleted += 1

    session.commit()
    logger.info(f"Issues synced: +{added} added, ~{updated} updated, -{deleted} deleted, ={total} total")
    return SyncStats(total=total, added=added, updated=updated, deleted=deleted)


def sync_snapshot_files_to_db(session: Session, slugs: list[SnapshotSlug]) -> SyncStats:
    """Sync snapshot_files table from snapshot content archives in DB."""

    # Get existing files from DB (primitive tuples to avoid detached ORM access)
    existing_keys: set[tuple[SnapshotSlug, str]] = {
        (row[0], row[1]) for row in session.execute(select(SnapshotFile.snapshot_slug, SnapshotFile.relative_path))
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
                    .values(snapshot_slug=slug, relative_path=relative, line_count=line_count)
                    .on_conflict_do_update(
                        index_elements=[SnapshotFile.snapshot_slug, SnapshotFile.relative_path],
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
    for snapshot_slug, relative_path in existing_keys - seen_keys:
        session.query(SnapshotFile).filter_by(snapshot_slug=snapshot_slug, relative_path=relative_path).delete()
        deleted += 1

    session.commit()
    logger.info(f"Snapshot files synced: +{added} added, ~{updated} updated, -{deleted} deleted, ={total} total")
    return SyncStats(total=total, added=added, updated=updated, deleted=deleted)


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
        true_positives, _ = load_yaml_issues(slug, specimens_dir)

        for tp in true_positives:
            for occurrence in tp.occurrences:
                for trigger_files in occurrence.critic_scopes_expected_to_recall:
                    # Convert to strings and compute hash
                    file_paths = [str(f) for f in trigger_files]
                    files_hash = compute_files_hash(file_paths)

                    key = (slug, files_hash)
                    desired_file_sets.setdefault(key, file_paths)
                    desired_triggers.add((slug, tp.tp_id, occurrence.occurrence_id, files_hash))

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
        # Clear graders_match_only_if_reported_on on occurrences before deleting file_set (FK RESTRICT)
        session.query(TruePositiveOccurrenceORM).filter_by(
            snapshot_slug=slug, graders_match_only_if_reported_on=files_hash
        ).update({TruePositiveOccurrenceORM.graders_match_only_if_reported_on: None})
        session.query(FalsePositiveOccurrenceORM).filter_by(
            snapshot_slug=slug, graders_match_only_if_reported_on=files_hash
        ).update({FalsePositiveOccurrenceORM.graders_match_only_if_reported_on: None})
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

    session.commit()
    logger.info(
        "File sets synced: +%d added, -%d deleted; critic_scopes_expected_to_recall +%d, -%d",
        file_sets_added,
        file_sets_deleted,
        critic_scopes_expected_to_recall_added,
        critic_scopes_expected_to_recall_deleted,
    )
    total = len(desired_file_sets)
    return SyncStats(total=total, added=file_sets_added, updated=0, deleted=file_sets_deleted)


# ============================================================================
# Model Metadata Sync (from MODEL_METADATA source of truth)
# ============================================================================


@dataclass
class ModelMetadataSyncStats:
    """Statistics from a model metadata sync operation."""

    total: int
    added: int
    updated: int
    deleted: int

    @property
    def summary_text(self) -> str:
        """Format as human-readable summary."""
        return f"{self.total} models (+{self.added}, ~{self.updated}, -{self.deleted})"


def sync_model_metadata() -> ModelMetadataSyncStats:
    """Sync model_metadata table from MODEL_METADATA source.

    Opens its own session internally (legacy interface for backward compatibility).

    Returns:
        Statistics about what changed
    """
    with get_session() as session:
        return sync_model_metadata_with_session(session)


def sync_model_metadata_with_session(session: Session) -> ModelMetadataSyncStats:
    """Sync model_metadata table from MODEL_METADATA source using provided session.

    Ensures database exactly matches the source of truth.

    Args:
        session: Active database session

    Returns:
        Statistics about what changed
    """
    # Fast path: if count matches, assume synced
    existing_count = session.query(ModelMetadata).count()
    if existing_count == len(MODEL_METADATA):
        logger.debug(f"Model metadata already synced ({existing_count} models)")
        return ModelMetadataSyncStats(added=0, updated=0, deleted=0, total=existing_count)

    # Full sync: make DB exactly match source
    logger.info(f"Syncing model_metadata table (source: {len(MODEL_METADATA)} models, DB: {existing_count})...")

    db_models = {m.model_id: m for m in session.query(ModelMetadata).all()}
    source_model_ids = set(MODEL_METADATA.keys())
    db_model_ids = set(db_models.keys())

    added = 0
    updated = 0
    deleted = 0

    # Delete orphaned models (in DB but not in source)
    for model_id in db_model_ids - source_model_ids:
        logger.info(f"  Deleting orphaned model: {model_id}")
        session.delete(db_models[model_id])
        deleted += 1

    # Add/update from source using merge (handles both cases)
    for model_id, meta in MODEL_METADATA.items():
        is_new = model_id not in db_model_ids
        session.merge(
            ModelMetadata(
                model_id=model_id,
                input_usd_per_1m_tokens=meta.input_usd_per_1m_tokens,
                cached_input_usd_per_1m_tokens=meta.cached_input_usd_per_1m_tokens,
                output_usd_per_1m_tokens=meta.output_usd_per_1m_tokens,
                context_window_tokens=meta.context_window_tokens,
                max_output_tokens=meta.max_output_tokens,
            )
        )
        if is_new:
            logger.debug(f"  Adding model: {model_id}")
            added += 1
        else:
            # Note: merge() updates if changed; count all as updated for stats
            updated += 1

    session.flush()

    logger.info(
        f"Model metadata synced: +{added} added, ~{updated} updated, -{deleted} deleted, ={len(MODEL_METADATA)} total"
    )
    return ModelMetadataSyncStats(added=added, updated=updated, deleted=deleted, total=len(MODEL_METADATA))


# ============================================================================
# Agent Definitions Sync (from repo-tracked agent_defs/)
# ============================================================================


# Detector definitions that inherit from critic (use critic agent_type)
CRITIC_BASED_DETECTORS = {
    "dead_code",
    "flag_propagation",
    "contract_truthfulness",
    "high_recall_critic",
    "verbose_docs",
}


# Agent definition sync removed - definitions are now OCI images managed via registry


# ============================================================================
# Full Sync (orchestrates all sync operations)
# ============================================================================


@dataclass
class FullSyncResult:
    """Combined result from syncing snapshots, issues, files, file sets, and model metadata.

    Note: Agent definitions are no longer synced - they are OCI images managed via registry.
    """

    snapshot_stats: SyncStats
    issue_stats: SyncStats
    snapshot_file_stats: SyncStats
    file_set_stats: SyncStats
    model_metadata_stats: ModelMetadataSyncStats


def sync_all(session: Session, *, use_staged: bool = False, dry_run: bool = False) -> FullSyncResult:
    """Sync snapshots, issues, files, file sets, and model metadata.

    Note: Agent definitions are no longer synced - they are OCI images managed via registry.

    Discovers snapshots once and passes data to all sync operations.
    All sync operations happen within the provided database session for consistency.

    Sync order is critical:
    1. snapshots (creates content archives from specimens repo)
    2. snapshot_files (reads from DB content column)
    3. issues (depends on snapshots)
    4. file_sets (depends on snapshot_files and issues via FK)
    5. model_metadata (independent)
    6. agent_definitions (independent)

    Args:
        session: Active database session
        use_staged: If True, read agent definitions from staged files instead of HEAD.
        dry_run: If True, rollback all changes instead of committing. Validates constraints.

    Returns:
        Combined results from all sync operations
    """
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

    # 3. Sync issues (true_positives/false_positives)
    print("Syncing issues...")
    issue_stats = sync_issues_to_db(session, slugs, specimens_dir)
    print(f"  {issue_stats.summary_text}")

    # 4. Sync file sets (examples VIEW is derived from these automatically)
    print("Syncing file sets...")
    file_set_stats = sync_file_sets_to_db(session, slugs, specimens_dir)
    print(f"  {file_set_stats.summary_text}")

    # 5. Sync model metadata
    print("Syncing model metadata...")
    model_metadata_stats = sync_model_metadata_with_session(session)
    print(f"  {model_metadata_stats.summary_text}")

    # Note: Agent definitions no longer synced (OCI images managed via registry)

    if dry_run:
        logger.info("DRY-RUN: Rolling back all changes")
        session.rollback()

    return FullSyncResult(
        snapshot_stats=snapshot_stats,
        issue_stats=issue_stats,
        snapshot_file_stats=snapshot_file_stats,
        file_set_stats=file_set_stats,
        model_metadata_stats=model_metadata_stats,
    )
