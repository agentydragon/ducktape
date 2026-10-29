"""Shared cache and hook-daemon socket paths for the statusline."""

from __future__ import annotations

from pathlib import Path

from platformdirs import user_cache_dir

# This XDG namespace stores shared hook/statusline state. Keep it stable across
# executable and wheel renames so existing caches and logs remain available.
SHARED_STATE_APPNAME = "claude-hooks"


def default_cache_dir() -> Path:
    """Resolve the shared Claude tooling cache directory (respects XDG_CACHE_HOME)."""
    return Path(user_cache_dir(appname=SHARED_STATE_APPNAME))


def _short_session_dir(session_id: str) -> Path:
    """Short session-scoped dir under /tmp for Unix sockets (108-byte AF_UNIX limit)."""
    return Path("/tmp/claude-hd") / session_id


def hook_daemon_sock(session_id: str) -> Path:
    """UDS path for the hook daemon, derived from session_id alone."""
    return _short_session_dir(session_id) / "d.sock"
