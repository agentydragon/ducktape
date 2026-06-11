"""Violation tracking for quality gate in stop hook."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config.models import Violation
from ..types import SessionID

if TYPE_CHECKING:
    from .manager import SessionManager

logger = logging.getLogger(__name__)


class ViolationTracker:
    """Tracks violations found during a session for quality gate.

    Violations are kept in-memory during the session. They are not persisted
    to disk as part of session data.
    """

    def __init__(self, session_manager: SessionManager) -> None:
        self.session_manager = session_manager
        self._violations: dict[
            SessionID, dict[tuple[Path, int, str], dict[str, Any]]
        ] = {}  # session_id -> {key -> violation_dict}

    def add_violation(
        self,
        session_id: SessionID,
        file_path: Path,
        line: int,
        message: str,
        severity: str = "error",
        rule: str | None = None,
    ) -> None:
        """Add a violation to the session."""
        if session_id not in self._violations:
            self._violations[session_id] = {}

        violation_dict = {
            "file_path": str(file_path),
            "line": line,
            "message": message,
            "severity": severity,
            "rule": rule,
            "timestamp": datetime.now().isoformat(),
            "fixed": False,
        }

        key = (file_path, line, message)
        self._violations[session_id][key] = violation_dict

    def add_violations(
        self, session_id: SessionID, violations: list[Violation], file_path: Path, severity: str = "error"
    ) -> None:
        """Add multiple violations from a linter."""
        for v in violations:
            self.add_violation(
                session_id=session_id,
                file_path=Path(v.file_path) if v.file_path else file_path,
                line=v.line,
                message=v.message,
                severity=severity,
                rule=v.rule,
            )

    def mark_file_fixed(self, session_id: SessionID, file_path: Path) -> None:
        """Mark all violations in a file as fixed."""
        if session_id not in self._violations:
            return

        file_path_str = str(file_path)
        for violation in self._violations[session_id].values():
            if violation["file_path"] == file_path_str:
                violation["fixed"] = True

    def get_unfixed_violations(self, session_id: SessionID) -> list[dict[str, Any]]:
        """Get all unfixed violations for a session."""
        violations = self._violations.get(session_id, {})
        return [v for v in violations.values() if not v["fixed"]]

    def get_violation_summary(self, session_id: SessionID) -> dict[str, Any]:
        """Get a summary of violations for the session."""
        unfixed = self.get_unfixed_violations(session_id)

        # Group by file
        by_file: dict[str, list[dict[str, Any]]] = {}
        for v in unfixed:
            fp = v["file_path"]
            if fp not in by_file:
                by_file[fp] = []
            by_file[fp].append(v)

        # Count by severity
        by_severity = {"error": 0, "warning": 0, "info": 0}
        for v in unfixed:
            sev = v.get("severity", "error")
            by_severity[sev] = by_severity.get(sev, 0) + 1

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
