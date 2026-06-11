"""Session state management for Claude Code hooks."""

from pathlib import Path

import platformdirs

from .claude_code_api import SessionID


def get_session_dir(hook_name: str, session_id: SessionID) -> Path:
    """Get the session-scoped directory for a hook, creating it if needed."""
    state_dir = platformdirs.user_state_dir("claude-hooks", ensure_exists=True)
    session_dir = Path(state_dir) / hook_name / str(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir
