#!/usr/bin/env python3
"""Scan Python codebase for all comments and docstrings.

This script extracts ALL comments (inline, block, docstrings) from Python files
with surrounding context for manual review.

Usage:
    python scan_comments.py [directory]

Output:
    JSON with comments grouped by file
"""

import ast
import json
import sys
from pathlib import Path
from typing import Any


def extract_comments_from_file(filepath: Path, lines: list[str]) -> list[dict[str, Any]]:
    """Extract all comments from a Python file with surrounding context.

    Returns list of dicts with:
        - line: Line number of comment
        - comment: Comment text (without # prefix)
        - context_before: 3 lines before comment
        - context_after: 3 lines after comment
        - type: "inline" | "block" | "docstring"
    """
    comments = []

    # Extract regular comments (# ...)
    for i, line in enumerate(lines, start=1):
        # Find comment marker
        if "#" not in line or line.strip().startswith("#!"):
            continue

        comment_idx = line.index("#")
        # Skip shebang
        if comment_idx == 0 and line.startswith("#!"):
            continue

        comment_text = line[comment_idx + 1 :].strip()
        context_before = "".join(lines[max(0, i - 4) : i - 1])
        context_after = "".join(lines[i : min(len(lines), i + 3)])

        # Determine if inline (has code before #) or block (starts line)
        before_comment = line[:comment_idx].strip()
        comment_type = "inline" if before_comment else "block"

        comments.append(
            {
                "line": i,
                "comment": comment_text,
                "context_before": context_before,
                "context_after": context_after,
                "type": comment_type,
            }
        )

    # Extract docstrings via AST
    try:
        tree = ast.parse("".join(lines), filename=str(filepath))
        # Only call get_docstring on nodes that can have docstrings
        for node in ast.walk(tree):
            if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                docstring = ast.get_docstring(node)
                if docstring:
                    # Module docstrings don't have lineno, they're always at line 1
                    if isinstance(node, ast.Module):
                        line_num = 1
                    elif hasattr(node, "lineno"):
                        line_num = node.lineno
                    else:
                        continue

                    context_before = "".join(lines[max(0, line_num - 4) : line_num - 1])
                    context_after = "".join(lines[line_num : min(len(lines), line_num + 10)])

                    comments.append(
                        {
                            "line": line_num,
                            "comment": docstring,
                            "context_before": context_before,
                            "context_after": context_after,
                            "type": "docstring",
                        }
                    )
    except SyntaxError:
        # Skip files with syntax errors
        pass

    return comments


def scan_file(filepath: Path) -> list[dict[str, Any]]:
    """Scan a single Python file for comments."""
    try:
        lines = filepath.read_text(encoding="utf-8").splitlines(keepends=True)
        return extract_comments_from_file(filepath, lines)
    except (UnicodeDecodeError, OSError):
        # Skip files with encoding issues or read errors
        return []


def scan_directory(root: Path) -> dict[str, Any]:
    """Scan all Python files in directory tree."""
    comments_by_file: dict[str, list[dict]] = {}
    total_comments = 0

    for py_file in root.rglob("*.py"):
        # Skip common non-source directories
        if any(part in py_file.parts for part in ["venv", "__pycache__", ".git", "node_modules", ".tox"]):
            continue

        comments = scan_file(py_file)

        if comments:
            comments_by_file[str(py_file)] = comments
            total_comments += len(comments)

    return {
        "summary": {"total_comments": total_comments, "files_with_comments": len(comments_by_file)},
        "comments": comments_by_file,
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
    print(f"Total comments/docstrings: {results['summary']['total_comments']}", file=sys.stderr)
    print(f"Files with comments: {results['summary']['files_with_comments']}", file=sys.stderr)


if __name__ == "__main__":
    main()
