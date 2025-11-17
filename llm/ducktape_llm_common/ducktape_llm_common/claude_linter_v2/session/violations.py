"""Violation tracking for quality gate in stop hook."""

from datetime import datetime
import logging
from typing import TYPE_CHECKING, Any

from ..config.models import Violation
from ..types import SessionID

if TYPE_CHECKING:
    from .manager import SessionManager

logger = logging.getLogger(__name__)


class ViolationTracker:
    """Tracks violations found during a session for quality gate."""

    def __init__(self, session_manager: SessionManager) -> None:
        self.session_manager = session_manager
        self._violations: dict[
            SessionID, dict[tuple[str, int, str], dict[str, Any]]
        ] = {}  # session_id -> {key -> violation_dict}

    def add_violation(
        self,
        session_id: SessionID,
        file_path: str,
        line: int,
        message: str,
        severity: str = "error",
        rule: str | None = None,
    ) -> None:
        """Add a violation to the session."""
        if session_id not in self._violations:
            self._violations[session_id] = {}

        # Create violation dict for storage
        violation_dict = {
            "file_path": file_path,
            "line": line,
            "message": message,
            "severity": severity,
            "rule": rule,
            "timestamp": datetime.now().isoformat(),
            "fixed": False,
        }

        # Use key for deduplication
        key = (file_path, line, message)
        self._violations[session_id][key] = violation_dict

        # Also persist to session data
        self._save_violations(session_id)

    def add_violations(
        self,
        session_id: SessionID,
        violations: list[Violation],  # Proper Violation objects from config.models
        file_path: str,
        severity: str = "error",
    ) -> None:
        """Add multiple violations from a linter."""
        for v in violations:
            # Now we have proper Violation objects with typed attributes
            self.add_violation(
                session_id=session_id,
                file_path=v.file_path or file_path,  # Use violation's file_path if available
                line=v.line,
                message=v.message,
                severity=severity,
                rule=v.rule,
            )

    def mark_file_fixed(self, session_id: SessionID, file_path: str) -> None:
        """Mark all violations in a file as fixed (e.g., after successful edit with no errors)."""
        if session_id not in self._violations:
            return

        for violation in self._violations[session_id].values():
            if violation.file_path == file_path:
                violation.fixed = True

        self._save_violations(session_id)

    def get_unfixed_violations(self, session_id: SessionID) -> list[Violation]:
        """Get all unfixed violations for a session."""
        if session_id not in self._violations:
            # Try to load from session data
            self._load_violations(session_id)

        violations = self._violations.get(session_id, {})
        return [v for v in violations.values() if not v.fixed]

    def get_violation_summary(self, session_id: SessionID) -> dict[str, Any]:
        """Get a summary of violations for the session."""
        unfixed = self.get_unfixed_violations(session_id)

        # Group by file
        by_file: dict[str, list[Violation]] = {}
        for v in unfixed:
            if v.file_path not in by_file:
                by_file[v.file_path] = []
            by_file[v.file_path].append(v)

        # Count by severity
        by_severity = {"error": 0, "warning": 0, "info": 0}
        for v in unfixed:
            by_severity[v.severity] = by_severity.get(v.severity, 0) + 1

        return {
            "total": len(unfixed),
            "by_severity": by_severity,
            "by_file": {file: len(violations) for file, violations in by_file.items()},
            "files_with_errors": list(by_file.keys()),
        }

    def clear_session(self, session_id: SessionID) -> None:
        """Clear all violations for a session."""
        if session_id in self._violations:
            del self._violations[session_id]

        # Also clear from session data
        session_data = self.session_manager._load_session(session_id)
        if "violations" in session_data:
            del session_data["violations"]
            self.session_manager._save_session(session_id, session_data)

    def _save_violations(self, session_id: SessionID) -> None:
        """Save violations to session data."""
        if session_id not in self._violations:
            return

        session_data = self.session_manager._load_session(session_id)
        # We're already storing dicts, so just convert to list
        session_data["violations"] = list(self._violations[session_id].values())
        self.session_manager._save_session(session_id, session_data)

    def _load_violations(self, session_id: SessionID) -> None:
        """Load violations from session data."""
        session_data = self.session_manager._load_session(session_id)
        violations_data = session_data.get("violations", [])

        violations_dict = {}
        for v_data in violations_data:
            # Reconstruct key from violation data
            key = (v_data["file_path"], v_data["line"], v_data["message"])
            violations_dict[key] = v_data
        self._violations[session_id] = violations_dict
