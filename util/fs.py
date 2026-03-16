"""Filesystem utilities."""

from __future__ import annotations

import contextlib
from collections.abc import Generator
from pathlib import Path


@contextlib.contextmanager
def restore_file(path: Path) -> Generator[None]:
    """Restore file content on exit, even if the block modifies it."""
    original = path.read_bytes()
    try:
        yield
    finally:
        path.write_bytes(original)
