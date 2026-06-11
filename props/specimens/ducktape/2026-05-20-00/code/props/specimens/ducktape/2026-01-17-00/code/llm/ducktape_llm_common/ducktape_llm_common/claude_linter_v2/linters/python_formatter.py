"""Python code formatter for selective autofix."""

import logging
import subprocess
import sys
from pathlib import Path

from ..config.models import AutofixCategory

logger = logging.getLogger(__name__)


class PythonFormatter:
    """Handles Python code formatting and autofixing."""

    def __init__(self, tools: list[str]) -> None:
        """
        Initialize formatter with specified tools.

        Args:
            tools: List of tools to use (e.g., ["ruff", "black"])
        """
        self.tools = tools
        self._available_tools = self._check_available_tools()

    def _check_available_tools(self) -> list[str]:
        """Check which formatting tools are available."""
        available = []
        for tool in self.tools:
            try:
                # Use python -m for ruff to work in Bazel sandbox
                cmd = [sys.executable, "-m", tool, "--version"] if tool == "ruff" else [tool, "--version"]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
                if result.returncode == 0:
                    available.append(tool)
                    logger.debug(f"Found {tool}: {result.stdout.strip()}")
            except (subprocess.SubprocessError, FileNotFoundError):
                logger.warning(f"Tool {tool} not available")

        return available

    def format_code(
        self, code: str, file_path: Path, categories: list[AutofixCategory] | None = None
    ) -> tuple[str, list[str]]:
        """Format Python code with specified autofix categories."""
        if not self._available_tools:
            logger.warning("No formatting tools available")
            return code, []

        # Default to formatting only if no categories specified
        if categories is None:
            categories = [AutofixCategory.FORMATTING]

        # Convert ALL to all categories
        if AutofixCategory.ALL in categories:
            categories = list(AutofixCategory)

        formatted_code = code
        changes = []

        # Apply formatting based on categories
        if AutofixCategory.FORMATTING in categories:
            formatted_code, formatting_changes = self._apply_formatting(formatted_code, file_path)
            changes.extend(formatting_changes)

        if AutofixCategory.IMPORTS in categories:
            formatted_code, import_changes = self._fix_imports(formatted_code, file_path)
            changes.extend(import_changes)

        if AutofixCategory.TYPE_HINTS in categories:
            # TODO: Implement type hint fixes (e.g., with pyupgrade)
            pass

        if AutofixCategory.SECURITY in categories:
            # TODO: Implement security fixes (e.g., bandit autofixes)
            pass

        return formatted_code, changes

    def _apply_formatting(self, code: str, file_path: Path) -> tuple[str, list[str]]:
        """Apply code formatting."""
        changes = []
        formatted = code

        # Try each available tool
        for tool in self._available_tools:
            if tool == "ruff":
                formatted, tool_changes = self._format_with_ruff(formatted, file_path)
                changes.extend(tool_changes)
            elif tool == "black":
                formatted, tool_changes = self._format_with_black(formatted, file_path)
                changes.extend(tool_changes)

        return formatted, changes

    def _format_with_ruff(self, code: str, file_path: Path) -> tuple[str, list[str]]:
        """Format code with ruff."""
        try:
            # Use stdin/stdout to avoid file operations
            # Use python -m ruff to work in Bazel sandbox
            result = subprocess.run(
                [sys.executable, "-m", "ruff", "format", "--stdin-filename", file_path, "-"],
                input=code,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            if result.returncode == 0:
                if result.stdout != code:
                    return result.stdout, ["Applied ruff formatting"]
                return code, []
            logger.warning(f"Ruff formatting failed: {result.stderr}")
            return code, []

        except subprocess.SubprocessError as e:
            logger.error(f"Ruff error: {e}")
            return code, []

    def _format_with_black(self, code: str, file_path: Path) -> tuple[str, list[str]]:
        """Format code with black."""
        try:
            # Use stdin/stdout to avoid file operations
            result = subprocess.run(
                ["black", "-", "--quiet"], input=code, capture_output=True, text=True, timeout=30, check=False
            )

            if result.returncode == 0:
                if result.stdout != code:
                    return result.stdout, ["Applied black formatting"]
                return code, []
            logger.warning(f"Black formatting failed: {result.stderr}")
            return code, []

        except subprocess.SubprocessError as e:
            logger.error(f"Black error: {e}")
            return code, []

    def _fix_imports(self, code: str, file_path: Path) -> tuple[str, list[str]]:
        """Fix import ordering and remove unused imports."""
        changes = []
        formatted = code

        if "ruff" in self._available_tools:
            # Ruff can fix imports with --fix
            # Use python -m ruff to work in Bazel sandbox
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "ruff",
                        "check",
                        "--fix",
                        "--select",
                        "I,F401",  # I=isort, F401=unused imports
                        "--stdin-filename",
                        file_path,
                        "-",
                    ],
                    input=formatted,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )

                # Ruff outputs the fixed code to stdout when using stdin
                if (
                    result.returncode in (0, 1) and result.stdout and result.stdout != formatted
                ):  # 1 = had issues but fixed them
                    formatted = result.stdout
                    changes.append("Fixed import ordering and removed unused imports")

            except subprocess.SubprocessError as e:
                logger.error(f"Ruff import fix error: {e}")

        return formatted, changes
