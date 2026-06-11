"""Session management for tracking Claude Code sessions and their permissions."""

import json
import logging
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from platformdirs import user_data_dir
from pydantic import BaseModel, ConfigDict, Field

from llm.claude_linter_v2.types import SessionID, parse_session_id


class RuleAction(StrEnum):
    """Permission rule actions."""

    ALLOW = "allow"
    DENY = "deny"


class Rule(BaseModel):
    """Session-specific permission rule."""

    model_config = ConfigDict(frozen=False)

    predicate: str
    action: RuleAction
    created: datetime
    expires: datetime | None = None


class SessionData(BaseModel):
    """Session data structure."""

    model_config = ConfigDict(frozen=False, arbitrary_types_allowed=True)

    id: SessionID
    created: datetime
    last_seen: datetime | None = None
    directory: Path | None = None
    rules: list[Rule] = Field(default_factory=list)
    notification_id: int | None = None


# Type alias for backwards compatibility
SessionInfo = SessionData

logger = logging.getLogger(__name__)


class SessionManager:
    """Manages Claude Code sessions and their permissions."""

    def __init__(self) -> None:
        # Store session data in platform-appropriate location
        self.data_dir = Path(user_data_dir("claude-linter-v2", "ducktape"))
        self.sessions_dir = self.data_dir / "sessions"
        self.projects_dir = self.data_dir / "projects"

        # Create directories
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.projects_dir.mkdir(parents=True, exist_ok=True)

    def _session_file(self, session_id: SessionID) -> Path:
        """Get the path to a session's data file."""
        return self.sessions_dir / f"{session_id}.json"

    def _load_session(self, session_id: SessionID) -> SessionData:
        """Load a single session from disk."""
        session_file = self._session_file(session_id)
        if session_file.exists():
            try:
                return SessionData.model_validate_json(session_file.read_text())
            except (json.JSONDecodeError, OSError, ValueError) as e:
                logger.error(f"Failed to load session {session_id}: {e}")

        # Return default session data
        return SessionData(id=session_id, created=datetime.now())

    def _save_session(self, session_id: SessionID, session_data: SessionData) -> None:
        """Save a single session to disk."""
        session_file = self._session_file(session_id)
        try:
            session_file.write_text(session_data.model_dump_json(indent=2))
        except (OSError, TypeError) as e:
            logger.error(f"Failed to save session {session_id}: {e}")

    def track_session(self, session_id: SessionID, working_dir: Path) -> None:
        """
        Track that a session is active.

        Args:
            session_id: Claude Code session ID
            working_dir: Current working directory for the session
        """
        session_data = self._load_session(session_id)
        session_data.last_seen = datetime.now()
        session_data.directory = working_dir.resolve()
        self._save_session(session_id, session_data)

    def set_notification_id(self, session_id: SessionID, notification_id: int) -> None:
        """Store the notification ID for a session."""
        session_data = self._load_session(session_id)
        session_data.notification_id = notification_id
        self._save_session(session_id, session_data)

    def get_notification_id(self, session_id: SessionID) -> int | None:
        """Get the notification ID for a session."""
        session_data = self._load_session(session_id)
        return session_data.notification_id

    def clear_notification_id(self, session_id: SessionID) -> None:
        """Clear the notification ID for a session."""
        session_data = self._load_session(session_id)
        session_data.notification_id = None
        self._save_session(session_id, session_data)

    def add_rule(
        self,
        predicate: str,
        action: RuleAction,
        expires: datetime | None = None,
        session_id: SessionID | None = None,
        directory: Path | None = None,
    ) -> int:
        """Add a permission rule to session(s)."""
        directory = directory or Path.cwd()
        directory_str = str(directory.resolve())

        rule = Rule(predicate=predicate, action=action, created=datetime.now(), expires=expires)

        affected = 0

        if session_id:
            # Add to specific session
            session_data = self._load_session(session_id)
            session_data.rules.append(rule)
            self._save_session(session_id, session_data)
            affected = 1
        else:
            # Add to all sessions in the directory
            for session_file in self.sessions_dir.glob("*.json"):
                sid = parse_session_id(session_file.stem)
                session_data = self._load_session(sid)

                # Skip if session is in different directory
                session_dir = str(session_data.directory) if session_data.directory else ""
                if not session_dir.startswith(directory_str):
                    continue

                # Add rule to this session
                session_data.rules.append(rule.model_copy())
                self._save_session(sid, session_data)
                affected += 1

        return affected

    def list_sessions(self, all_dirs: bool = False) -> list[SessionData]:
        """List all sessions."""
        current_dir = Path.cwd().resolve()
        results = []

        # Scan all session files
        for session_file in self.sessions_dir.glob("*.json"):
            session_id = parse_session_id(session_file.stem)
            session_data = self._load_session(session_id)

            # Skip sessions in other directories unless requested
            if not all_dirs and session_data.directory and not session_data.directory.is_relative_to(current_dir):
                continue

            results.append(session_data)

        # Sort by last seen time (most recent first)
        results.sort(key=lambda x: x.last_seen or x.created, reverse=True)

        return results

    def get_session_rules(self, session_id: SessionID) -> list[Rule]:
        """Get active rules for a session."""
        session_data = self._load_session(session_id)

        # Filter out expired rules
        now = datetime.now()
        active_rules = []

        for rule in session_data.rules:
            if rule.expires and rule.expires < now:
                continue
            active_rules.append(rule)

        return active_rules

    @staticmethod
    def sanitize_path(path: Path) -> str:
        """Sanitize a path for use as a directory name (like Claude does)."""
        # Convert to absolute path and replace / with -
        abs_path = str(path.resolve())
        return abs_path.replace("/", "-")

    def clear_turn_data(self, session_id: SessionID) -> None:
        """
        Clear any turn-specific data.

        Note: Sessions persist across multiple Claude turns.
        This is called when Claude ends a turn, not when a session ends.
        """
        # Currently a no-op but available for future turn-specific cleanup
