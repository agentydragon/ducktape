"""Python code formatter for selective autofix."""

import logging
import subprocess
from pathlib import Path

from llm.claude_linter_v2.config.models import AutofixCategory
from llm.claude_linter_v2.linters.ruff_binary import find_ruff_binary

logger = logging.getLogger(__name__)


class PythonFormatter:
    """Handles Python code formatting and autofixing."""

    # Tools that are valid python_tools but not formatters (skip silently)
    _NON_FORMATTING_TOOLS: frozenset[str] = frozenset({"mypy"})

    def __init__(self, tools: list[str]) -> None:
        self._formatting_tools = [t for t in tools if t not in self._NON_FORMATTING_TOOLS]
        self._ruff_bin = find_ruff_binary() if "ruff" in self._formatting_tools else None
        self._available_tools = self._check_available_tools()

    def _check_available_tools(self) -> list[str]:
        """Check which formatting tools are available. Raises if a configured tool is missing."""
        available = []
        for tool in self._formatting_tools:
            if tool == "ruff":
                if not self._ruff_bin:
                    raise RuntimeError(
                        "ruff is configured as a formatting tool but the binary was not found. "
                        "Set RUFF_BIN env var or add ruff to PATH, or remove 'ruff' from python_tools config."
                    )
                available.append(tool)
                logger.debug(f"Found ruff: {self._ruff_bin}")
            elif tool == "black":
                try:
                    result = subprocess.run(
                        ["black", "--version"], capture_output=True, text=True, timeout=5, check=False
                    )
                    if result.returncode != 0:
                        raise RuntimeError(f"black --version returned exit code {result.returncode}")
                    available.append(tool)
                    logger.debug(f"Found black: {result.stdout.strip()}")
                except (subprocess.SubprocessError, FileNotFoundError) as e:
                    raise RuntimeError(
                        "black is configured as a formatting tool but is not available. "
                        "Install black or remove it from python_tools config."
                    ) from e
            else:
                raise RuntimeError(f"Unknown formatting tool: {tool!r}")

        return available

    def format_code(
        self, code: str, file_path: Path, categories: list[AutofixCategory] | None = None
    ) -> tuple[str, list[str]]:
        """Format Python code with specified autofix categories."""
        if not self._available_tools:
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

        return formatted_code, changes

    def _apply_formatting(self, code: str, file_path: Path) -> tuple[str, list[str]]:
        """Apply code formatting."""
        changes = []
        formatted = code

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
        assert self._ruff_bin
        try:
            result = subprocess.run(
                [self._ruff_bin, "format", "--stdin-filename", str(file_path), "-"],
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
        if "ruff" not in self._available_tools:
            return code, []

        assert self._ruff_bin
        changes = []
        formatted = code

        try:
            result = subprocess.run(
                [
                    self._ruff_bin,
                    "check",
                    "--fix",
                    "--select",
                    "I,F401",  # I=isort, F401=unused imports
                    "--stdin-filename",
                    str(file_path),
                    "-",
                ],
                input=formatted,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            # Ruff outputs the fixed code to stdout when using stdin
            if result.returncode in (0, 1) and result.stdout and result.stdout != formatted:
                formatted = result.stdout
                changes.append("Fixed import ordering and removed unused imports")

        except subprocess.SubprocessError as e:
            logger.error(f"Ruff import fix error: {e}")

        return formatted, changes
