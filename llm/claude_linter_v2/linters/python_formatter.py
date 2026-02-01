"""Python code formatter for selective autofix."""

import logging
import subprocess
from pathlib import Path

from llm.claude_linter_v2.config.models import AutofixCategory
from llm.claude_linter_v2.linters.ruff_binary import find_ruff_binary

logger = logging.getLogger(__name__)


class PythonFormatter:
    """Handles Python code formatting and autofixing via ruff."""

    # Tools that are valid python_tools but not formatters (skip silently)
    _NON_FORMATTING_TOOLS: frozenset[str] = frozenset({"mypy"})

    def __init__(self, tools: list[str]) -> None:
        formatting_tools = [t for t in tools if t not in self._NON_FORMATTING_TOOLS]
        for tool in formatting_tools:
            if tool != "ruff":
                raise RuntimeError(f"Unknown formatting tool: {tool!r}. Only 'ruff' is supported.")
        self._use_ruff = "ruff" in formatting_tools
        if self._use_ruff:
            ruff_bin = find_ruff_binary()
            if not ruff_bin:
                raise RuntimeError(
                    "ruff is configured as a formatting tool but the binary was not found. "
                    "Set RUFF_BIN env var or add ruff to PATH, or remove 'ruff' from python_tools config."
                )
            self._ruff_bin: str = ruff_bin

    def format_code(
        self, code: str, file_path: Path, categories: list[AutofixCategory] | None = None
    ) -> tuple[str, list[str]]:
        """Format Python code with specified autofix categories."""
        if not self._use_ruff:
            return code, []

        # Default to formatting only if no categories specified
        if categories is None:
            categories = [AutofixCategory.FORMATTING]

        # Convert ALL to all categories
        if AutofixCategory.ALL in categories:
            categories = list(AutofixCategory)

        formatted_code = code
        changes: list[str] = []

        if AutofixCategory.FORMATTING in categories:
            formatted_code, formatting_changes = self._apply_formatting(formatted_code, file_path)
            changes.extend(formatting_changes)

        if AutofixCategory.IMPORTS in categories:
            formatted_code, import_changes = self._fix_imports(formatted_code, file_path)
            changes.extend(import_changes)

        return formatted_code, changes

    def _apply_formatting(self, code: str, file_path: Path) -> tuple[str, list[str]]:
        """Format code with ruff."""
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

    def _fix_imports(self, code: str, file_path: Path) -> tuple[str, list[str]]:
        """Fix import ordering and remove unused imports."""
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
                input=code,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            if result.returncode in (0, 1) and result.stdout and result.stdout != code:
                return result.stdout, ["Fixed import ordering and removed unused imports"]

        except subprocess.SubprocessError as e:
            logger.error(f"Ruff import fix error: {e}")

        return code, []
