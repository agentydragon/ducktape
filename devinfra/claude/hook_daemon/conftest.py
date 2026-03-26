"""Shared fixtures for hook daemon tests."""

import tempfile
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest

from devinfra.claude.session_paths import SessionPaths


@pytest.fixture
def short_tmp() -> Generator[Path]:
    """Short temp dir to avoid AF_UNIX 108-byte path limit in Bazel sandboxes."""
    with tempfile.TemporaryDirectory(prefix="hd-", dir="/tmp") as d:
        yield Path(d)


@pytest.fixture
def daemon_paths(tmp_path: Path) -> SessionPaths:
    """SessionPaths with a unique session_id (isolates socket path between tests).

    Each test gets its own session_id, so daemon socket paths don't collide.
    Daemons left running after a test are harmless — they'll be killed when
    the RBE container exits.
    """
    session_id = f"td-{uuid.uuid4().hex[:8]}"
    paths = SessionPaths(session_id=session_id, home=tmp_path, xdg_cache_home=tmp_path / "cache")
    (tmp_path / "cache").mkdir()
    return paths
