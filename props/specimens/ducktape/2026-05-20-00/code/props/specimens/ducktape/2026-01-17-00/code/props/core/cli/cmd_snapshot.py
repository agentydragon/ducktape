"""Snapshot management commands: list, dump, exec, shell, capture-ducktape, fetch."""

from __future__ import annotations

import fnmatch
import io
import json
import shutil
import tarfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Annotated

import pygit2
import typer
import yaml
from typer_di import TyperDI

from cli_util.decorators import async_run
from props.core.cli import common_options as opt
from props.core.db.models import Snapshot
from props.core.db.session import get_session
from props.core.db.sync.export import _format_files
from props.core.db.sync.sync import get_specimens_base_path
from props.core.ids import SnapshotSlug

# Snapshot subcommand group
snapshot_app = TyperDI(help="Snapshot commands")


def _apply_gitignore_patterns(
    file_list: list[str], include: Sequence[str] = (), exclude: Sequence[str] = ()
) -> list[str]:
    """Apply gitignore-style include/exclude patterns to a file list.

    Include patterns are applied first (whitelist), then exclude patterns (blacklist).

    Args:
        file_list: List of file paths to filter
        include: Patterns to include (if specified, only matching files are kept)
        exclude: Patterns to exclude (matching files are removed)

    Returns:
        Filtered list of file paths

    Example:
        >>> _apply_gitignore_patterns(
        ...     ["adgn/src/foo.py", "adgn/tests/test.py", "wt/bar.py"],
        ...     include=["adgn/"],
        ...     exclude=["adgn/tests/"]
        ... )
        ["adgn/src/foo.py"]
    """

    def matches_pattern(path: str, pattern: str) -> bool:
        """Check if path matches gitignore-style pattern."""
        # Remove trailing slash from pattern (indicates directory)
        if pattern.endswith("/"):
            pattern = pattern.rstrip("/")
            # For directory patterns, match the directory and everything under it
            return path.startswith(pattern + "/") or path == pattern
        # For file patterns, use fnmatch
        return fnmatch.fnmatch(path, pattern) or path.startswith(pattern + "/")

    def matches_any_pattern(path: str, patterns: Sequence[str]) -> bool:
        """Check if path matches any of the given patterns."""
        return any(matches_pattern(path, pattern) for pattern in patterns)

    result = file_list

    # Apply include patterns (if specified, only keep matching files)
    if include:
        result = [f for f in result if matches_any_pattern(f, include)]

    # Apply exclude patterns (remove matching files)
    if exclude:
        result = [f for f in result if not matches_any_pattern(f, exclude)]

    return result


def copy_working_tree_files(
    repo: pygit2.Repository, dest_dir: Path, include: Sequence[str] = (), exclude: Sequence[str] = ()
) -> list[str]:
    """Copy files from working tree to destination directory.

    Includes:
    - Tracked files (in git index)
    - Untracked non-gitignored files

    Args:
        repo: pygit2 Repository
        dest_dir: Destination directory (will be created if doesn't exist)
        include: Gitignore-style include patterns
        exclude: Gitignore-style exclude patterns

    Returns:
        List of copied file paths (relative to repo root)
    """
    repo_root = Path(repo.workdir)

    tracked_files = [entry.path for entry in repo.index]

    # repo.status() returns dict[path, status_flags]; GIT_STATUS_WT_NEW means untracked but not ignored
    status_dict = repo.status(untracked_files="all", ignored=False)
    untracked_files = [filepath for filepath, flags in status_dict.items() if flags & pygit2.GIT_STATUS_WT_NEW]

    # Combine and apply filters
    all_files = tracked_files + untracked_files
    filtered_files = _apply_gitignore_patterns(all_files, include=include, exclude=exclude)

    # Copy files
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = []

    for rel_path in filtered_files:
        src = repo_root / rel_path
        dst = dest_dir / rel_path

        if not src.exists():
            typer.echo(f"WARNING: Skipping non-existent file: {rel_path}", err=True)
            continue

        # Create parent directories
        dst.parent.mkdir(parents=True, exist_ok=True)

        # Copy file (preserve metadata)
        if src.is_symlink():
            # Preserve symlinks as-is
            link_target = src.readlink()
            dst.symlink_to(link_target)
        else:
            shutil.copy2(src, dst)

        copied.append(rel_path)

    return copied


@async_run
async def cmd_snapshot_list() -> None:
    """List all valid snapshot slugs."""
    with get_session() as session:
        snapshots = session.query(Snapshot).all()
        slugs = sorted([s.slug for s in snapshots])

    for slug in slugs:
        typer.echo(str(slug))


@async_run
async def snapshot_dump(
    snapshot: SnapshotSlug = opt.ARG_SNAPSHOT,
    pretty: bool = typer.Option(True, help="Pretty-print JSON with indentation"),
) -> None:
    """Dump a snapshot's full structure as JSON (manifest, all issues, occurrences)."""
    try:
        # Load snapshot and issues from database (no source hydration needed for dump)
        with get_session() as session:
            db_snapshot = session.query(Snapshot).filter_by(slug=snapshot).one()

            # Build output structure directly from ORM
            output = {
                "slug": str(db_snapshot.slug),
                "issues": {
                    tp.tp_id: {
                        "rationale": tp.rationale,
                        "instances": [
                            {
                                "occurrence_id": occ.occurrence_id,
                                "files": _format_files(occ.ranges),
                                "note": occ.note,
                                "critic_scopes_expected_to_recall": [
                                    sorted(str(m.file_path) for m in scope.file_set.members)
                                    for scope in occ.critic_scopes_expected_to_recall
                                    if scope.file_set
                                ],
                            }
                            for occ in tp.occurrences
                        ],
                    }
                    for tp in db_snapshot.true_positives
                },
                "false_positives": {
                    fp.fp_id: {
                        "rationale": fp.rationale,
                        "instances": [
                            {
                                "occurrence_id": occ.occurrence_id,
                                "files": _format_files(occ.ranges),
                                "note": occ.note,
                                "relevant_files": sorted(str(rf.file_path) for rf in occ.relevant_file_orms),
                            }
                            for occ in fp.occurrences
                        ],
                    }
                    for fp in db_snapshot.false_positives
                },
            }

            indent = 2 if pretty else None
            print(json.dumps(output, indent=indent))
    except Exception as e:
        typer.echo(f"ERROR: Failed to load snapshot '{snapshot}': {e}")
        raise typer.Exit(2) from e


def snapshot_exec() -> None:
    """Execute a command in a container with snapshot mounted at /workspace (RW).

    TODO: Delete this command or reimplement to fetch snapshot from database.
    Hydration was removed - snapshots are now stored as blobs in PostgreSQL.
    """
    raise NotImplementedError(
        "snapshot exec is temporarily disabled. "
        "Hydration was removed - snapshots are now stored in PostgreSQL. "
        "Consider deleting this command or reimplementing with database fetch."
    )


def snapshot_shell() -> None:
    """Open an interactive bash shell in a container with snapshot mounted at /workspace (RW).

    TODO: Delete this command or reimplement to fetch snapshot from database.
    Hydration was removed - snapshots are now stored as blobs in PostgreSQL.
    """
    raise NotImplementedError(
        "snapshot shell is temporarily disabled. "
        "Hydration was removed - snapshots are now stored in PostgreSQL. "
        "Consider deleting this command or reimplementing with database fetch."
    )


def snapshot_capture_ducktape(
    slug: Annotated[
        str | None, typer.Option(help="Snapshot slug (e.g., 'ducktape/2025-11-30-00'); auto-generated if not provided")
    ] = None,
    include: Annotated[list[str] | None, typer.Option(help="Paths to include (repeatable)")] = None,
    exclude: Annotated[list[str] | None, typer.Option(help="Paths to exclude (repeatable)")] = None,
) -> None:
    """Capture current ducktape repo state as a new snapshot (plain files).

    Copies files from working tree (tracked + untracked non-gitignored) to specimens repo.
    Creates snapshot with issues/ and code/ subdirectories. Does NOT auto-commit.
    """
    # Set defaults for mutable list arguments (match recent ducktape snapshots)
    if include is None:
        include = ["adgn/"]
    if exclude is None:
        exclude = ["props/"]

    # Get current commit SHA using pygit2
    # Discover repository from current directory (should be within ducktape repo)
    repo_path = pygit2.discover_repository(str(Path.cwd()))
    if not repo_path:
        raise typer.BadParameter("Could not find git repository. Run from within ducktape repo.")
    repo = pygit2.Repository(repo_path)
    source_commit = str(repo.head.target)

    # Generate slug if not provided
    if slug is None:
        today = datetime.now().strftime("%Y-%m-%d")
        with get_session() as session:
            snapshots = session.query(Snapshot).all()
            existing = sorted([s.slug for s in snapshots if str(s.slug).startswith(f"ducktape/{today}")])
        next_num = len(existing)
        slug = f"ducktape/{today}-{next_num:02d}"

    # Create snapshot directory
    snapshot_dir = get_specimens_base_path() / slug
    if snapshot_dir.exists():
        raise typer.BadParameter(f"Snapshot directory already exists: {snapshot_dir}")
    snapshot_dir.mkdir(parents=True, exist_ok=False)

    # Create issues/ directory
    issues_dir = snapshot_dir / "issues"
    issues_dir.mkdir()

    # Create code/ directory and copy working tree files
    code_dir = snapshot_dir / "code"
    typer.echo(f"Copying working tree files to {code_dir}...")
    copied_files = copy_working_tree_files(repo, code_dir, include=include, exclude=exclude)
    typer.echo(f"✓ Copied {len(copied_files)} files")

    # Write manifest.yaml in the snapshot directory
    manifest_path = snapshot_dir / "manifest.yaml"
    manifest_data = {
        "source": {"vcs": "local", "root": "code"},
        "split": "train",  # Default split, user can change manually
        "bundle": {"source_commit": source_commit, "include": list(include), "exclude": list(exclude)},
    }

    with manifest_path.open("w") as f:
        yaml.dump(manifest_data, f, default_flow_style=False, sort_keys=False)

    typer.echo()
    typer.echo(f"✓ Snapshot captured: {slug}")
    typer.echo(f"  Snapshot directory: {snapshot_dir}")
    typer.echo(f"  Source commit: {source_commit}")
    typer.echo(f"  Copied files: {len(copied_files)}")
    typer.echo(f"  Include patterns: {include}")
    typer.echo(f"  Exclude patterns: {exclude}")
    typer.echo()
    typer.echo("Next steps:")
    typer.echo(f"  1. Review captured files in {code_dir}")
    typer.echo(f"  2. Add issues to {issues_dir}/")
    typer.echo("  3. Review and commit changes in specimens repo")
    typer.echo()
    typer.echo("Note: Changes are NOT auto-committed. Review before committing.")


def fetch_snapshot_to_path(slug: str, output: Path) -> None:
    """Fetch snapshot from database and extract to filesystem.

    Retrieves the tar archive from the snapshots table and extracts it
    to the specified output directory.

    Args:
        slug: Snapshot slug (e.g., 'ducktape/2025-11-26-00')
        output: Output directory to extract snapshot into

    Raises:
        ValueError: If snapshot not found or has no content
    """
    output.mkdir(parents=True, exist_ok=True)

    with get_session() as session:
        snapshot = session.query(Snapshot).filter_by(slug=slug).first()
        if snapshot is None:
            raise ValueError(f"Snapshot not found: {slug}")
        if snapshot.content is None:
            raise ValueError(f"Snapshot has no content: {slug}")

        archive_bytes = snapshot.content

    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r") as tf:
        tf.extractall(output, filter="data")


def snapshot_fetch(
    slug: Annotated[str, typer.Argument(help="Snapshot slug (e.g., 'ducktape/2025-11-26-00')")],
    output: Annotated[Path, typer.Argument(help="Output directory to extract snapshot into")],
) -> None:
    """Fetch snapshot from database and extract to filesystem.

    Retrieves the tar archive from the snapshots table and extracts it
    to the specified output directory. Used by agent init scripts to
    fetch snapshots into containers.

    Example:
        props snapshot fetch ducktape/2025-11-26-00 /snapshots/ducktape/2025-11-26-00
    """
    try:
        fetch_snapshot_to_path(slug, output)
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Extracted: {output}")


# Register commands
snapshot_app.command("list")(cmd_snapshot_list)
snapshot_app.command("dump")(snapshot_dump)
snapshot_app.command("exec")(snapshot_exec)
snapshot_app.command("shell")(snapshot_shell)
snapshot_app.command("capture-ducktape")(snapshot_capture_ducktape)
snapshot_app.command("fetch")(snapshot_fetch)
