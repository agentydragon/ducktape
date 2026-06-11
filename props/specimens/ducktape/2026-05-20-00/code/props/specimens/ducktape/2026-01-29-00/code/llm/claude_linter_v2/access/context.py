"""Context object for predicate evaluation."""

import fnmatch
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from llm.claude_linter_v2.types import SessionID


@dataclass
class PredicateContext:
    """
    Context provided to predicate functions for evaluation.

    Simple structure with tool name, arguments, session info, and timestamp.
    """

    tool: str  # Tool name: Write, Edit, MultiEdit, Read, Bash, etc.
    args: dict[str, Any]  # Tool arguments as key-value pairs
    session_id: SessionID
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def path(self) -> str | None:
        """Get file_path from args for convenience."""
        return self.args.get("file_path")

    @property
    def content(self) -> str | None:
        """Get content from args for convenience."""
        return self.args.get("content")

    @property
    def command(self) -> str | None:
        """Get command from args for convenience."""
        return self.args.get("command")

    def glob_match(self, pattern: str) -> bool:
        """Check if path matches a glob pattern."""
        if not self.path:
            return False
        return fnmatch.fnmatch(self.path, pattern)
