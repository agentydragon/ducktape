"""Direct file checking for cl2 check command."""

import logging
from pathlib import Path

from .config.loader import ConfigLoader
from .config.models import AutofixCategory, Violation
from .linters.python_ast import PythonASTAnalyzer
from .linters.python_formatter import PythonFormatter
from .linters.python_ruff import PythonRuffLinter

logger = logging.getLogger(__name__)


class FileChecker:
    """Checks files for violations and optionally fixes them."""

    def __init__(
        self, fix: bool = False, categories: list[AutofixCategory] | None = None, verbose: bool = False
    ) -> None:
        """
        Initialize the file checker.

        Args:
            fix: Whether to fix issues
            categories: Autofix categories to apply (empty = all)
            verbose: Enable verbose output
        """
        self.fix = fix
        self.categories = categories or []
        self.verbose = verbose

        # Load config
        self.config_loader = ConfigLoader()
        self.config = self.config_loader.config

    def check_file(self, file_path: Path) -> list[Violation]:
        """
        Check a single file for violations.

        Args:
            file_path: Path to the file to check

        Returns:
            List of violations found
        """
        violations: list[Violation] = []

        # Only check Python files for now
        if file_path.suffix != ".py":
            return violations

        try:
            content = file_path.read_text()
        except (OSError, UnicodeDecodeError) as e:
            logger.error(f"Failed to read {file_path}: {e}")
            return violations

        # Run AST checks
        bare_except_config = self.config.get_rule_config("python.bare_except")
        hasattr_config = self.config.get_rule_config("python.hasattr")
        getattr_config = self.config.get_rule_config("python.getattr")
        setattr_config = self.config.get_rule_config("python.setattr")
        barrel_init_config = self.config.get_rule_config("python.barrel_init")

        analyzer = PythonASTAnalyzer(
            bare_except=bare_except_config.enabled if bare_except_config else False,
            getattr_setattr=(
                (hasattr_config.enabled if hasattr_config else False)
                or (getattr_config.enabled if getattr_config else False)
                or (setattr_config.enabled if setattr_config else False)
            ),
            barrel_init=file_path.name == "__init__.py"
            and (barrel_init_config.enabled if barrel_init_config else False),
        )
        ast_violations = analyzer.analyze_code(content, file_path)
        violations.extend(ast_violations)

        # Run ruff checks
        ruff_linter = PythonRuffLinter(force_select=self.config.get_ruff_codes_to_select())
        ruff_violations = ruff_linter.check_code(content, file_path, critical_only=False)
        violations.extend(ruff_violations)

        # Apply fixes if requested
        if self.fix and self.categories:
            formatter = PythonFormatter(self.config.python_tools)
            formatted_content, changes = formatter.format_code(content, file_path, self.categories)

            if changes and formatted_content != content:
                try:
                    file_path.write_text(formatted_content)
                    if self.verbose:
                        logger.info(f"Applied fixes to {file_path}: {', '.join(changes)}")

                    # Re-check after fixing to get updated violations
                    violations = self.check_file(file_path)
                except OSError as e:
                    logger.error(f"Failed to write fixes to {file_path}: {e}")

        return violations
