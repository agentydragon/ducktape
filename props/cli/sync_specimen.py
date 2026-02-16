#!/usr/bin/env python3
"""Sync a specimen bundle to the props database.

This CLI tool syncs a single specimen from bundle artifacts (code tar + data YAML).

Usage:
    # Sync from Bazel-generated bundle artifacts
    props-sync-specimen \\
        --slug ducktape/2026-01-17-00 \\
        --code-tar bazel-bin/props/specimens/ducktape/2026-01-17-00/specimen_code.tar \\
        --data-yaml bazel-bin/props/specimens/ducktape/2026-01-17-00/specimen_data.yaml

The code tar must be an uncompressed tar file containing the code/ directory.
The data YAML must be a merged YAML file with {split, issues} structure.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from props.db.config import load_database_config
from props.db.database import Database
from props.db.sync.sync import SpecimenBundle, sync_all


def sync_from_bundle(slug: str, code_tar: Path, data_yaml: Path, db: Database) -> None:
    """Sync specimen from bundle artifacts.

    Args:
        slug: Specimen slug (e.g., "ducktape/2026-01-17-00")
        code_tar: Path to uncompressed code tar
        data_yaml: Path to merged data YAML (split + issues)
        db: Database instance
    """
    # Create bundle
    bundle = SpecimenBundle(slug=slug, code_tar=code_tar, data_yaml=data_yaml)

    # Sync using bundle workflow
    print(f"Syncing {slug} from bundle artifacts...")
    with db.session() as session:
        result = sync_all(session, specimen_bundles=[bundle])

    print(f"  Snapshots: {result.snapshots}")
    print(f"  Issues: {result.issues}")
    print(f"  Files: {result.files}")
    print(f"  File sets: {result.file_sets}")
    print(f"  Model metadata: {result.model_metadata}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync a specimen bundle to the props database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Bundle artifacts (required)
    parser.add_argument("--slug", required=True, help="Specimen slug (e.g., ducktape/2026-01-17-00)")
    parser.add_argument("--code-tar", type=Path, required=True, help="Path to uncompressed code tar")
    parser.add_argument("--data-yaml", type=Path, required=True, help="Path to merged data YAML (split + issues)")

    args = parser.parse_args()

    # Load database config
    try:
        db_config = load_database_config()
        db = Database(db_config)
    except Exception as e:
        print(f"Error connecting to database: {e}", file=sys.stderr)
        return 1

    # Sync
    try:
        sync_from_bundle(args.slug, args.code_tar, args.data_yaml, db)
        print("✓ Sync completed successfully")
        return 0

    except Exception as e:
        print(f"Error during sync: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1
    finally:
        db.dispose()


if __name__ == "__main__":
    sys.exit(main())
