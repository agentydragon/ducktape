#!/usr/bin/env python3
"""Sync a specimen bundle to the props database.

This CLI tool syncs a single specimen (code + issues + manifest) to the database.
It can be run with or without Bazel.

Usage:
    # Sync from extracted bundle directory
    props-sync-specimen /path/to/specimens/ducktape/2026-01-17-00

    # Sync from Bazel-generated artifacts
    props-sync-specimen \\
        --code-tar bazel-bin/props/specimens/ducktape/2026-01-17-00/specimen_code.tar.gz \\
        --issues-dir props/specimens/ducktape/2026-01-17-00/issues \\
        --manifest props/specimens/ducktape/2026-01-17-00/manifest.yaml \\
        --slug ducktape/2026-01-17-00

The tool creates a temporary directory, extracts/copies the bundle, and syncs it
using the same sync_all() path as tests and batch sync.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

from props.db.config import load_database_config
from props.db.database import Database
from props.db.sync.sync import sync_all


def sync_from_directory(specimen_dir: Path, db: Database) -> None:
    """Sync specimen from a directory structure.

    Expected structure:
        {specimen_dir}/
            code/...
            issues/**/*.yaml
            manifest.yaml
    """
    if not specimen_dir.exists():
        raise ValueError(f"Specimen directory not found: {specimen_dir}")

    if not (specimen_dir / "manifest.yaml").exists():
        raise ValueError(f"manifest.yaml not found in {specimen_dir}")

    # sync_all expects specimens_root to be parent of {repo}/{date}
    # If specimen_dir is /path/ducktape/2026-01-17-00, we need /path
    specimens_root = specimen_dir.parent.parent

    with db.session() as session:
        sync_all(session, use_staged=True, collect_errors=True)


def sync_from_artifacts(
    slug: str, code_tar: Path, issues_dir: Path, manifest: Path, db: Database
) -> None:
    """Sync specimen from separate artifacts (Bazel-generated).

    Creates temporary directory with expected structure, then syncs.
    """
    repo, date = slug.split("/")

    with tempfile.TemporaryDirectory(prefix=f"specimen_{slug.replace('/', '_')}_") as tmpdir:
        tmp_path = Path(tmpdir)
        specimen_path = tmp_path / repo / date
        code_path = specimen_path / "code"
        code_path.mkdir(parents=True, exist_ok=True)

        # Extract code tar
        print(f"Extracting {code_tar} to {code_path}")
        with tarfile.open(code_tar, "r:gz") as tar:
            tar.extractall(code_path)

        # Copy manifest
        print(f"Copying {manifest}")
        shutil.copy2(manifest, specimen_path / "manifest.yaml")

        # Copy issues directory
        issues_dest = specimen_path / "issues"
        if issues_dir.exists() and issues_dir.is_dir():
            print(f"Copying {issues_dir}")
            shutil.copytree(issues_dir, issues_dest)

        # Sync using sync_all
        print(f"Syncing {slug} to database...")
        with db.session() as session:
            sync_all(session, use_staged=True, collect_errors=True, specimens_root=tmp_path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync a specimen bundle to the props database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Mode 1: Directory
    parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        help="Path to specimen directory (contains code/, issues/, manifest.yaml)",
    )

    # Mode 2: Separate artifacts
    parser.add_argument("--slug", help="Specimen slug (e.g., ducktape/2026-01-17-00)")
    parser.add_argument("--code-tar", type=Path, help="Path to code tar.gz")
    parser.add_argument("--manifest", type=Path, help="Path to manifest.yaml")
    parser.add_argument("--issues-dir", type=Path, help="Path to issues directory")

    args = parser.parse_args()

    # Validate arguments
    if args.directory:
        if any([args.slug, args.code_tar, args.manifest, args.issues_dir]):
            parser.error("Cannot mix directory mode with artifact mode")
        mode = "directory"
    elif all([args.slug, args.code_tar, args.manifest]):
        mode = "artifacts"
    else:
        parser.error("Either provide directory or (slug + code-tar + manifest)")

    # Load database config
    try:
        db_config = load_database_config()
        db = Database(db_config)
    except Exception as e:
        print(f"Error connecting to database: {e}", file=sys.stderr)
        return 1

    # Sync
    try:
        if mode == "directory":
            print(f"Syncing from directory: {args.directory}")
            sync_from_directory(args.directory, db)
        else:
            print(f"Syncing from artifacts: {args.slug}")
            sync_from_artifacts(args.slug, args.code_tar, args.issues_dir or Path("/nonexistent"), args.manifest, db)

        print("✓ Sync completed successfully")
        return 0

    except Exception as e:
        print(f"Error during sync: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1
    finally:
        db.dispose()


if __name__ == "__main__":
    sys.exit(main())
