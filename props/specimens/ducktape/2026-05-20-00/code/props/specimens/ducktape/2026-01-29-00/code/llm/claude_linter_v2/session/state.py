"""Session state management for Claude Linter v2."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from llm.claude_linter_v2.config.models import Violation
from llm.claude_linter_v2.types import SessionID


class Rule(BaseModel):
    """Session-specific permission rule."""

    model_config = ConfigDict(frozen=False)

    predicate: str
    action: str
    created: datetime
    expires: datetime | None = None


@dataclass
class SessionState:
    """Encapsulates all state for a single session."""

    session_id: SessionID
    working_directory: Path
    created_at: datetime = field(default_factory=datetime.now)
    last_seen: datetime = field(default_factory=datetime.now)

    # Files touched since last Stop hook
    touched_files: set[Path] = field(default_factory=set)

    # Warnings to show in post-hook
    pending_warnings: list[str] = field(default_factory=list)

    # Session-specific rules
    rules: list[Rule] = field(default_factory=list)

    # Notification tracking
    notification_id: int | None = None

    # Violation tracking (for informational purposes only)
    # The Stop hook will do fresh scans, not rely on this
    historical_violations: dict[str, list[Violation]] = field(default_factory=dict)

    def touch_file(self, file_path: Path) -> None:
        """Mark a file as touched in this session."""
        self.touched_files.add(file_path)
        self.last_seen = datetime.now()

    def add_warning(self, warning: str) -> None:
        """Add a warning to show in post-hook."""
        self.pending_warnings.append(warning)
        self.last_seen = datetime.now()

    def consume_warnings(self) -> list[str]:
        """Get and clear pending warnings."""
        warnings = self.pending_warnings.copy()
        self.pending_warnings.clear()
        return warnings

    def clear_touched_files(self) -> None:
        """Clear the list of touched files (called after Stop hook)."""
        self.touched_files.clear()

    def add_rule(self, rule: Rule) -> None:
        """Add a session-specific rule."""
        self.rules.append(rule)
        self.last_seen = datetime.now()

    def set_notification_id(self, notification_id: int) -> None:
        """Set the current notification ID."""
        self.notification_id = notification_id
        self.last_seen = datetime.now()

    def clear_notification_id(self) -> None:
        """Clear the notification ID."""
        self.notification_id = None

    def update_last_seen(self) -> None:
        """Update the last seen timestamp."""
        self.last_seen = datetime.now()
