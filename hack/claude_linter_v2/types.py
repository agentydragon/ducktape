"""Common types for claude-linter-v2."""

from uuid import UUID

from llm.claude_code_api import SessionID


def parse_session_id(session_id_str: str) -> SessionID:
    """Parse a session ID string into a SessionID."""
    return SessionID(UUID(session_id_str))
