"""Crash-durable, exclusive marker files for uncertain session operations."""

from __future__ import annotations

import os
from pathlib import Path

from atomicwrites import atomic_write


def sync_directory(directory: Path) -> None:
    """Persist directory-entry changes after unlinking a marker."""
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class DurableUncertaintyMarker:
    """An empty durable file whose presence fences potentially rotated credentials.

    Creation uses atomicwrites with overwrite disabled, which provides exclusive
    admission and syncs the file and its parent directory before returning.
    Clearing still needs an unlink followed by a parent-directory sync: readers
    interpret absence as permission to reuse credentials, so that transition
    must be durable too.
    Existing contents are intentionally not parsed: any existing or unreadable
    marker remains uncertain until an explicit successful import clears it.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def is_set(self) -> bool:
        try:
            self.path.lstat()
        except FileNotFoundError:
            return False
        return True

    def mark_exclusive(self) -> None:
        """Durably create the marker or raise FileExistsError if already set."""
        with atomic_write(self.path, mode="wb", overwrite=False) as marker:
            marker.write(b"")

    def ensure_set(self) -> bool:
        """Durably set the marker if absent; return whether this call created it."""
        try:
            self.mark_exclusive()
        except FileExistsError:
            # StaticControl historically synced the parent directory even
            # when the marker already existed; keep this operation durable if
            # an earlier attempt created the entry before a sync error.
            sync_directory(self.path.parent)
            return False
        return True

    def clear(self) -> bool:
        """Remove a present marker and sync its parent directory."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        sync_directory(self.path.parent)
        return True
