"""Minimal snapshot I/O for in-container use.

This module provides snapshot fetching with minimal dependencies,
suitable for use inside agent containers without pulling in CLI deps.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from props.db.models import Snapshot
from props.db.session import get_session


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
