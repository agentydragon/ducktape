#!/usr/bin/env python3
"""Scan Python codebase for error handling antipatterns.

This script finds problematic exception handling patterns including bare excepts,
overly broad exception catching, and error swallowing.

Usage:
    python scan_error_handling.py [directory]

Output:
    JSON with error handling issues grouped by file
"""

import ast
import json
import sys
from pathlib import Path
from typing import Any


class ErrorHandlingAnalyzer(ast.NodeVisitor):
    """Analyze exception handling patterns."""

    def __init__(self):
        self.bare_except: list[dict[str, Any]] = []
        self.broad_except: list[dict[str, Any]] = []
        self.non_raising_except: list[dict[str, Any]] = []
        self.single_line_try: list[dict[str, Any]] = []

    def visit_Try(self, node: ast.Try) -> None:
        """Analyze try-except blocks."""
        # Check if try body is single statement (excluding pass/docstring)
        meaningful_stmts = [s for s in node.body if not isinstance(s, ast.Pass | ast.Expr)]
        if len(meaningful_stmts) == 1:
            self.single_line_try.append({"line": node.lineno, "col": node.col_offset})

        # Analyze each exception handler
        for handler in node.handlers:
            handler_info = {"line": handler.lineno, "col": handler.col_offset}

            # Check for bare except (no exception type)
            if handler.type is None:
                self.bare_except.append(handler_info)
            # Check for except Exception (too broad)
            elif isinstance(handler.type, ast.Name) and handler.type.id == "Exception":
                self.broad_except.append(handler_info)

            # Check if handler has any path that doesn't raise
            if self._has_non_raising_path(handler.body):
                self.non_raising_except.append(handler_info)

        self.generic_visit(node)

    def _has_non_raising_path(self, body: list[ast.stmt]) -> bool:
        """Check if exception handler has any path that doesn't raise.

        Returns True if handler contains:
        - return, break, continue, or pass
        - OR no Raise statements at all

        This will have false positives (e.g., logging then re-raising),
        but that's intentional - we want high recall.
        """
        has_raise = False
        has_swallowing = False

        for stmt in body:
            # Check for return/break/continue/pass
            if isinstance(stmt, ast.Return | ast.Break | ast.Continue | ast.Pass):
                has_swallowing = True

            # Check for raise (including bare raise for re-raising)
            if isinstance(stmt, ast.Raise):
                has_raise = True

            # Recursively check nested blocks
            if isinstance(stmt, ast.If):
                # Check both branches
                if self._has_non_raising_path(stmt.body):
                    has_swallowing = True
                if stmt.orelse and self._has_non_raising_path(stmt.orelse):
                    has_swallowing = True

        # If no raise statement at all, or has swallowing statements
        return not has_raise or has_swallowing


def scan_file(filepath: Path) -> dict[str, list[dict[str, Any]]]:
    """Scan a single Python file for error handling issues."""
    try:
        source = filepath.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(filepath))

        analyzer = ErrorHandlingAnalyzer()
        analyzer.visit(tree)

        result = {}
        if analyzer.bare_except:
            result["bare_except"] = analyzer.bare_except
        if analyzer.broad_except:
            result["broad_except"] = analyzer.broad_except
        if analyzer.non_raising_except:
            result["non_raising_except"] = analyzer.non_raising_except
        if analyzer.single_line_try:
            result["single_line_try"] = analyzer.single_line_try

        return result
    except (SyntaxError, UnicodeDecodeError):
        # Skip files with syntax errors or encoding issues
        return {}


def scan_directory(root: Path) -> dict[str, Any]:
    """Scan all Python files in directory tree."""
    issues_by_file: dict[str, dict[str, list[dict[str, Any]]]] = {}

    total_bare = 0
    total_broad = 0
    total_non_raising = 0
    total_single_line = 0

    for py_file in root.rglob("*.py"):
        # Skip common non-source directories
        if any(part in py_file.parts for part in ["venv", "__pycache__", ".git", "node_modules", ".tox"]):
            continue

        issues = scan_file(py_file)

        if issues:
            issues_by_file[str(py_file)] = issues

            total_bare += len(issues.get("bare_except", []))
            total_broad += len(issues.get("broad_except", []))
            total_non_raising += len(issues.get("non_raising_except", []))
            total_single_line += len(issues.get("single_line_try", []))

    return {
        "summary": {
            "bare_except": total_bare,
            "broad_except": total_broad,
            "non_raising_except": total_non_raising,
            "single_line_try": total_single_line,
        },
        "issues": issues_by_file,
    }


def main() -> None:
    """Main entry point."""
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()

    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning {root}...", file=sys.stderr)
    results = scan_directory(root)

    # Output JSON
    print(json.dumps(results, indent=2))

    # Print summary to stderr
    print("\n=== Summary ===", file=sys.stderr)
    print(f"Bare except: {results['summary']['bare_except']}", file=sys.stderr)
    print(f"Broad except: {results['summary']['broad_except']}", file=sys.stderr)
    print(f"Non-raising except: {results['summary']['non_raising_except']}", file=sys.stderr)
    print(f"Single-line try: {results['summary']['single_line_try']}", file=sys.stderr)


if __name__ == "__main__":
    main()
