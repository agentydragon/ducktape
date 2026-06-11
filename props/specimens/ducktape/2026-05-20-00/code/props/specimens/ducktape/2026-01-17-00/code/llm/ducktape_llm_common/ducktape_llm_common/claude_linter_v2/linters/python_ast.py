"""Python AST analyzer for detecting hard-blocked patterns."""

import ast
import logging
from pathlib import Path

from ..config.models import Violation

logger = logging.getLogger(__name__)


class PythonASTAnalyzer:
    """Analyzes Python AST for hard-blocked patterns."""

    def __init__(self, bare_except: bool = True, getattr_setattr: bool = True, barrel_init: bool = True) -> None:
        """
        Initialize the analyzer.

        Args:
            bare_except: Check for bare except clauses
            getattr_setattr: Check for hasattr/getattr/setattr usage
            barrel_init: Check for barrel __init__.py files
        """
        self.bare_except = bare_except
        self.getattr_setattr = getattr_setattr
        self.barrel_init = barrel_init

    def analyze_file(self, file_path: str | Path) -> list[Violation]:
        """
        Analyze a Python file for violations.

        Args:
            file_path: Path to the Python file

        Returns:
            List of violations found
        """
        file_path = Path(file_path)

        try:
            with file_path.open(encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError) as e:
            logger.error(f"Failed to read {file_path}: {e}")
            return []

        return self.analyze_code(content, file_path)

    def analyze_code(self, code: str, filename: Path) -> list[Violation]:
        """
        Analyze Python code for violations.

        Args:
            code: Python source code
            filename: Filename for error messages

        Returns:
            List of violations found
        """
        violations = []

        try:
            tree = ast.parse(code, filename)
        except SyntaxError as e:
            # Syntax errors prevent other checks
            return [
                Violation(
                    line=e.lineno or 1,
                    column=e.offset or 0,
                    message=f"Syntax error: {e.msg}",
                    rule="syntax",
                    fixable=False,
                    file_path=str(filename),
                )
            ]

        # Check for various patterns
        if self.bare_except:
            violations.extend(self._check_bare_except(tree))

        if self.getattr_setattr:
            violations.extend(self._check_getattr_setattr(tree))

        if self.barrel_init and filename.name == "__init__.py":
            violations.extend(self._check_barrel_init(tree, code))

        return violations

    def _check_bare_except(self, tree: ast.AST) -> list[Violation]:
        """Check for bare except clauses."""
        violations = []

        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                violations.append(
                    Violation(
                        line=node.lineno,
                        column=node.col_offset,
                        message="Bare except clause is not allowed. Use specific exception types.",
                        rule="bare_except",
                        fixable=False,
                        file_path=None,
                    )
                )

        return violations

    def _check_getattr_setattr(self, tree: ast.AST) -> list[Violation]:
        """Check for hasattr/getattr/setattr usage."""
        violations = []
        banned_functions = {"hasattr", "getattr", "setattr"}

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in banned_functions:
                violations.append(
                    Violation(
                        line=node.lineno,
                        column=node.col_offset,
                        message=f"Use of {node.func.id} is not allowed. Use proper type checking instead.",
                        rule="getattr_setattr",
                        fixable=False,
                        file_path=None,
                    )
                )

        return violations

    def _check_barrel_init(self, tree: ast.AST, code: str) -> list[Violation]:
        """
        Check for barrel __init__.py patterns.

        A barrel __init__.py is one that imports and re-exports everything,
        typically with patterns like:
        - from .module import *
        - from .module import Class; __all__ = ['Class']
        - Multiple imports that are immediately re-exported
        """
        violations = []

        # Check for star imports
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
                violations.append(
                    Violation(
                        line=node.lineno,
                        column=node.col_offset,
                        message="Barrel __init__.py with star imports is not allowed. Keep __init__.py minimal.",
                        rule="barrel_init",
                        fixable=False,
                        file_path=None,
                    )
                )

        # Check for re-export pattern
        imports: set[str] = set()
        exports: set[str] = set()

        for node in ast.walk(tree):
            # Collect imports
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name != "*":
                        imports.add(alias.asname or alias.name)

            # Check for __all__ assignment
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__all__" and isinstance(node.value, ast.List):
                        # Extract names from __all__
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                exports.add(elt.value)
                            elif isinstance(elt, ast.Str) and isinstance(elt.s, str):  # Python 3.7 compatibility
                                exports.add(elt.s)

        # If we have imports and they're all in __all__, it's a barrel
        if imports and exports and imports.issubset(exports):
            # Check if the file is mostly imports/exports
            lines = code.strip().split("\n")
            non_empty_lines = [line for line in lines if line.strip() and not line.strip().startswith("#")]

            if len(non_empty_lines) > 0:
                import_lines = sum(1 for line in non_empty_lines if "import" in line or "__all__" in line)
                if import_lines / len(non_empty_lines) > 0.7:  # More than 70% import/export
                    violations.append(
                        Violation(
                            line=1,
                            column=0,
                            message="Barrel __init__.py pattern detected. Keep __init__.py files minimal or empty.",
                            rule="barrel_init",
                            fixable=False,
                            file_path=None,
                        )
                    )

        return violations
