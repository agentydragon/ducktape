#!/usr/bin/env python3
"""Scan Python codebase for functions with single-line bodies.

This script finds functions (sync and async) that have exactly one line of
real code in their body (excluding docstrings). These are candidates for
trivial forwarders that should be inlined.

Usage:
    python scan_single_line_functions.py [directory]

Output:
    JSON with:
    - functions: [{name, file, line, statement, decorators, signature}]
    - summary: counts and statistics
"""

import ast
import json
import sys
from pathlib import Path
from typing import Any


class SingleLineExtractor(ast.NodeVisitor):
    """Extract functions with single-line bodies (excluding docstrings)."""

    def __init__(self, filepath: Path, lines: list[str]):
        self.filepath = filepath
        self.lines = lines
        self.single_line_functions: list[dict[str, Any]] = []

    def _get_decorators(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
        """Extract decorator names from function."""
        decorators = []
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Name):
                decorators.append(decorator.id)
            elif isinstance(decorator, ast.Attribute):
                decorators.append(decorator.attr)
            elif isinstance(decorator, ast.Call):
                if isinstance(decorator.func, ast.Name):
                    decorators.append(f"{decorator.func.id}(...)")
                elif isinstance(decorator.func, ast.Attribute):
                    decorators.append(f"{decorator.func.attr}(...)")
        return decorators

    def _get_signature(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        """Extract function signature from source."""
        try:
            start_line = node.lineno - 1
            # Find the end of the signature (look for colon)
            for i in range(start_line, min(start_line + 10, len(self.lines))):
                line = self.lines[i]
                if ":" in line:
                    # Extract from 'def' to ':'
                    signature_lines = self.lines[start_line : i + 1]
                    signature = "".join(signature_lines).strip()
                    # Remove decorators if present
                    if signature.startswith("@"):
                        # Skip decorator lines
                        signature = signature[signature.index("def") :]
                    return signature
            return f"def {node.name}(...)"
        except Exception:
            return f"def {node.name}(...)"

    def _count_real_statements(self, body: list[ast.stmt]) -> tuple[int, ast.stmt | None]:
        """Count real statements in function body (excluding docstrings).

        Returns (count, first_real_statement).
        """
        if not body:
            return 0, None

        # Check if first statement is a docstring
        first_stmt = body[0]
        start_idx = 0

        if (
            isinstance(first_stmt, ast.Expr)
            and isinstance(first_stmt.value, ast.Constant)
            and isinstance(first_stmt.value.value, str)
        ):
            # First statement is docstring, skip it
            start_idx = 1

        # Count remaining statements
        real_body = body[start_idx:]
        if not real_body:
            return 0, None

        # Filter out 'pass' statements
        non_pass_statements = [stmt for stmt in real_body if not isinstance(stmt, ast.Pass)]

        if len(non_pass_statements) == 0:
            return 0, None
        if len(non_pass_statements) == 1:
            return 1, non_pass_statements[0]
        return len(non_pass_statements), None

    def _statement_to_string(self, stmt: ast.stmt) -> str:
        """Convert AST statement to readable string."""
        try:
            # Get the source line(s) for this statement
            if hasattr(stmt, "lineno") and hasattr(stmt, "end_lineno"):
                start = stmt.lineno - 1
                end = stmt.end_lineno
                statement_lines = self.lines[start:end]
                return "".join(statement_lines).strip()
            return ast.unparse(stmt)
        except Exception:
            return f"<{type(stmt).__name__}>"

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, is_async: bool) -> None:
        """Common logic for sync and async functions."""
        count, first_stmt = self._count_real_statements(node.body)

        if count == 1 and first_stmt is not None:
            # This is a single-line function
            self.single_line_functions.append(
                {
                    "name": node.name,
                    "file": str(self.filepath),
                    "line": node.lineno,
                    "is_async": is_async,
                    "statement": self._statement_to_string(first_stmt),
                    "decorators": self._get_decorators(node),
                    "signature": self._get_signature(node),
                }
            )

        # Continue visiting nested functions
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Visit sync function definition."""
        self._visit_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Visit async function definition."""
        self._visit_function(node, is_async=True)


def scan_file(filepath: Path) -> list[dict[str, Any]]:
    """Scan a single Python file for single-line functions.

    Returns:
        List of single-line function dicts
    """
    try:
        content = filepath.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=True)
        tree = ast.parse(content, filename=str(filepath))
        extractor = SingleLineExtractor(filepath, lines)
        extractor.visit(tree)
        return extractor.single_line_functions
    except (SyntaxError, UnicodeDecodeError):
        return []


def scan_directory(directory: Path) -> dict[str, Any]:
    """Scan all Python files in directory."""
    all_functions = []

    for filepath in directory.rglob("*.py"):
        # Skip venv, .git, etc.
        if any(part.startswith(".") or part == "venv" for part in filepath.parts):
            continue

        functions = scan_file(filepath)
        all_functions.extend(functions)

    # Group by file
    by_file: dict[str, list[dict]] = {}
    for func in all_functions:
        file_path = func["file"]
        if file_path not in by_file:
            by_file[file_path] = []
        by_file[file_path].append(func)

    # Sort by file, then by line number
    for file_funcs in by_file.values():
        file_funcs.sort(key=lambda f: f["line"])

    return {
        "summary": {
            "total_single_line_functions": len(all_functions),
            "files_with_single_line_functions": len(by_file),
        },
        "functions": all_functions,
        "by_file": by_file,
    }


def main():
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()

    if not directory.exists():
        print(f"Error: Directory {directory} does not exist", file=sys.stderr)
        sys.exit(1)

    result = scan_directory(directory)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
