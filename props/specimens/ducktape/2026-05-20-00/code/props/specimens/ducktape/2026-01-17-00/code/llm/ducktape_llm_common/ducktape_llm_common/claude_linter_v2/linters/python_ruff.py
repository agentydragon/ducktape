"""Python ruff linter for pre-hook checks."""

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

from ..config.models import Violation

logger = logging.getLogger(__name__)


class PythonRuffLinter:
    """Runs ruff checks for Python code quality."""

    # Critical rules that should block in pre-hook
    CRITICAL_RULES: ClassVar[frozenset[str]] = frozenset(
        {
            # From v1 config - these are the most important ones
            "E722",  # bare-except
            "BLE001",  # blind-except
            "B009",  # getattr-with-constant
            "B010",  # setattr-with-constant
            "S113",  # request-without-timeout
            "B008",  # function-call-in-default-argument
            "E402",  # module-import-not-at-top-of-file
            "PLC0415",  # import-outside-top-level
            "S608",  # hardcoded-sql-expression
            "S611",  # django-raw-sql
            "B904",  # raise-without-from-inside-except
            "B006",  # mutable-argument-default
            "PGH003",  # blanket-type-ignore
        }
    )

    def __init__(self, force_select: list[str] | None = None) -> None:
        """
        Initialize ruff linter.

        Args:
            force_select: List of ruff rules to force enable
        """
        self.force_select = force_select or []
        self._ruff_available = self._check_ruff_available()

    def _check_ruff_available(self) -> bool:
        """Check if ruff is available."""
        try:
            result = subprocess.run(
                [sys.executable, "-m", "ruff", "--version"], capture_output=True, text=True, timeout=5, check=False
            )
            if result.returncode == 0:
                logger.debug(f"Found ruff: {result.stdout.strip()}")
                return True
        except (subprocess.SubprocessError, FileNotFoundError):
            logger.warning("ruff not available")
        return False

    def check_code(self, code: str, file_path: Path, critical_only: bool = True) -> list[Violation]:
        """
        Check Python code with ruff.

        Args:
            code: Python code to check
            file_path: File path for context
            critical_only: If True, only return critical violations

        Returns:
            List of violations found
        """
        if not self._ruff_available:
            logger.warning("ruff not available, skipping checks")
            return []

        violations = []

        # Build ruff command (use python -m ruff to work in Bazel sandbox)
        cmd = [sys.executable, "-m", "ruff", "check", "--output-format", "json", "--stdin-filename", str(file_path)]

        # Add force-select rules if provided
        if self.force_select:
            cmd.extend(["--select", ",".join(self.force_select)])
        elif critical_only:
            # Only check critical rules in pre-hook
            cmd.extend(["--select", ",".join(self.CRITICAL_RULES)])

        # Add stdin marker
        cmd.append("-")

        try:
            result = subprocess.run(cmd, input=code, capture_output=True, text=True, timeout=30, check=False)

            # Ruff returns 1 if violations found, 0 if clean
            if result.returncode in (0, 1):
                if result.stdout:
                    # Parse JSON output
                    try:
                        issues = json.loads(result.stdout)
                        for issue in issues:
                            # Skip if not critical and we're in critical_only mode
                            if critical_only and issue.get("code") not in self.CRITICAL_RULES:
                                continue

                            violation = Violation(
                                rule=f"ruff:{issue.get('code', 'unknown')}",
                                line=issue.get("location", {}).get("row", 0),
                                column=issue.get("location", {}).get("column", 0),
                                message=issue.get("message", "Unknown violation"),
                                fixable=issue.get("fix") is not None,
                                file_path=str(file_path),
                            )
                            violations.append(violation)
                    except json.JSONDecodeError:
                        logger.error(f"Failed to parse ruff output: {result.stdout}")
            else:
                logger.error(f"ruff failed with code {result.returncode}: {result.stderr}")

        except subprocess.SubprocessError as e:
            logger.error(f"ruff error: {e}")

        return violations

    def get_rule_explanation(self, rule_code: str) -> str:
        """Get explanation for a ruff rule."""
        explanations = {
            "E722": "Bare except catches all exceptions including system exits. Use specific exception types.",
            "BLE001": "Catching Exception is too broad. Catch specific exceptions.",
            "B009": "Use obj.attr instead of getattr(obj, 'attr') with constant string.",
            "B010": "Use obj.attr = value instead of setattr(obj, 'attr', value) with constant string.",
            "S113": "Requests without timeout can hang indefinitely. Add timeout parameter.",
            "B008": "Function calls in default arguments are evaluated once at definition time.",
            "E402": "Module imports should be at the top of the file.",
            "PLC0415": "Import statements should be at module level, not inside functions.",
            "S608": "SQL queries should use parameterized queries, not string formatting.",
            "S611": "Django raw SQL is vulnerable to injection. Use ORM or parameterized queries.",
            "B904": "Use 'raise ... from err' inside except blocks to preserve traceback.",
            "B006": "Mutable default arguments are shared between calls. Use None and create inside function.",
            "PGH003": "Use specific type: ignore comments like # type: ignore[attr-defined]",
        }

        return explanations.get(rule_code, f"Ruff rule {rule_code} violation")
