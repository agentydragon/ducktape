#!/usr/bin/env python3
"""Sync a specimen bundle to the props database.

This CLI tool syncs a single specimen from bundle artifacts (code tar + data YAML).

Usage:
    props-sync-specimen \\
        --code-tar bazel-bin/props/specimens/ducktape/2026-01-17-00/specimen_code.tar \\
        --data-yaml bazel-bin/props/specimens/ducktape/2026-01-17-00/specimen_data.yaml

The code tar must be an uncompressed tar file containing the code/ directory.
The data YAML must be a merged YAML file with {snapshot_slug, split, issues} structure.
The snapshot slug is read from the data YAML.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from props.db.config import load_database_config
from props.db.database import Database
from props.db.sync.sync import SpecimenBundle, sync_specimen


def sync_from_bundle(code_tar: Path, data_yaml: Path, db: Database) -> None:
    """Sync specimen from bundle artifacts.

    Args:
        code_tar: Path to uncompressed code tar
        data_yaml: Path to merged data YAML (snapshot_slug + split + issues)
        db: Database instance
    """
    # Create bundle (reads slug from data YAML)
    bundle = SpecimenBundle.from_paths(code_tar, data_yaml)

    print(f"Syncing {bundle.slug} from bundle artifacts...")
    with db.session() as session:
        sync_specimen(session, bundle)
        session.commit()

    print("✓ Sync completed successfully")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync a specimen bundle to the props database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Bundle artifacts (required)
    parser.add_argument("--code-tar", type=Path, required=True, help="Path to uncompressed code tar")
    parser.add_argument(
        "--data-yaml", type=Path, required=True, help="Path to merged data YAML (snapshot_slug + split + issues)"
    )

    args = parser.parse_args()

    # Load database config and sync (let errors propagate naturally)
    db_config = load_database_config()
    db = Database(db_config)
    try:
        sync_from_bundle(args.code_tar, args.data_yaml, db)
        return 0
    finally:
        db.dispose()


if __name__ == "__main__":
    sys.exit(main())
