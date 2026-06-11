#!/usr/bin/env python3
"""Scan Python codebase for classes that should be dataclasses or Pydantic models.

This script finds classes with boilerplate __init__ methods and manual dunder methods
that could be replaced with @dataclass or Pydantic BaseModel.

Usage:
    python scan_dataclass_candidates.py [directory]

Output:
    JSON with classes grouped by file
"""

import ast
import json
import sys
from pathlib import Path
from typing import Any


class DataclassAnalyzer(ast.NodeVisitor):
    """Analyze classes for dataclass/Pydantic conversion candidates."""

    def __init__(self):
        self.classes: dict[str, dict[str, Any]] = {}

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Analyze each class definition."""
        class_info = {
            "line": node.lineno,
            "init_self_assignments": 0,
            "init_other_statements": 0,
            "has_repr": False,
            "has_eq": False,
            "has_hash": False,
        }

        # Find __init__ and analyze its body
        init_method = None
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                init_method = item
            elif isinstance(item, ast.FunctionDef):
                # Check for dunder methods
                if item.name == "__repr__":
                    class_info["has_repr"] = True
                elif item.name == "__eq__":
                    class_info["has_eq"] = True
                elif item.name == "__hash__":
                    class_info["has_hash"] = True

        if init_method:
            # Analyze __init__ body
            for stmt in init_method.body:
                if self._is_self_assignment(stmt):
                    class_info["init_self_assignments"] += 1
                else:
                    # Not a simple self.x = x assignment
                    class_info["init_other_statements"] += 1

        # Store class info
        self.classes[node.name] = class_info

        self.generic_visit(node)

    def _is_self_assignment(self, stmt: ast.stmt) -> bool:
        """Check if statement is a simple self.x = y pattern."""
        if not isinstance(stmt, ast.Assign):
            return False

        # Check if single target
        if len(stmt.targets) != 1:
            return False

        target = stmt.targets[0]

        # Check if target is self.something
        if not isinstance(target, ast.Attribute):
            return False

        # This is a self.x = ... assignment
        # Could be self.x = x, self.x = x or None, self.x = list(x), etc.
        # We count it as a self-assignment (dataclass would handle it)
        return isinstance(target.value, ast.Name) and target.value.id == "self"


def scan_file(filepath: Path) -> dict[str, dict[str, Any]]:
    """Scan a single Python file for dataclass candidates."""
    try:
        source = filepath.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(filepath))

        analyzer = DataclassAnalyzer()
        analyzer.visit(tree)

        return analyzer.classes
    except (SyntaxError, UnicodeDecodeError):
        # Skip files with syntax errors or encoding issues
        return {}


def scan_directory(root: Path) -> dict[str, Any]:
    """Scan all Python files in directory tree."""
    classes_by_file: dict[str, dict[str, dict[str, Any]]] = {}
    total_classes = 0
    total_candidates = 0

    for py_file in root.rglob("*.py"):
        # Skip common non-source directories
        if any(part in py_file.parts for part in ["venv", "__pycache__", ".git", "node_modules", ".tox"]):
            continue

        classes = scan_file(py_file)

        if classes:
            classes_by_file[str(py_file)] = classes
            total_classes += len(classes)

            # Count candidates (5+ self assignments)
            candidates = sum(1 for info in classes.values() if info["init_self_assignments"] >= 5)
            total_candidates += candidates

    return {
        "summary": {"total_classes": total_classes, "candidate_classes": total_candidates},
        "classes": classes_by_file,
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
    print(f"Total classes: {results['summary']['total_classes']}", file=sys.stderr)
    print(f"Dataclass candidates (5+ params): {results['summary']['candidate_classes']}", file=sys.stderr)


if __name__ == "__main__":
    main()
