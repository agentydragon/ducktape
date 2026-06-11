"""Minimal snapshot I/O for in-container use.

This module provides snapshot fetching with minimal dependencies,
suitable for use inside agent containers without pulling in CLI deps.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from props.db.database import Database
from props.db.models import Snapshot


def fetch_snapshot_to_path(slug: str, output: Path, db: Database) -> None:
    """Fetch snapshot from database and extract to filesystem.

    Raises:
        ValueError: If snapshot not found or has no content
    """
    output.mkdir(parents=True, exist_ok=True)

    with db.session() as session:
        snapshot = session.query(Snapshot).filter_by(slug=slug).first()
        if snapshot is None:
            raise ValueError(f"Snapshot not found: {slug}")
        if snapshot.content is None:
            raise ValueError(f"Snapshot has no content: {slug}")

        archive_bytes = snapshot.content

    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r") as tf:
        tf.extractall(output, filter="data")
