#!/usr/bin/env python3
"""Scan Python codebase for all comments and docstrings.

This script extracts ALL comments (inline, block, docstrings) from Python files
with surrounding context for manual review.

Usage (from the repository root):
    python -m prompts.scans.scan_comments [directory]
    bb run //prompts/scans:scan_comments_bin -- [directory]

Output:
    JSON with comments grouped by file
"""

import ast
import sys
from pathlib import Path
from typing import Any

from prompts.scans.scan_runner import iter_python_files, run_cli


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
                    line_num = 1 if isinstance(node, ast.Module) else node.lineno

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
    except UnicodeDecodeError, OSError:
        # Skip files with encoding issues or read errors
        return []


def scan_directory(root: Path) -> dict[str, Any]:
    """Scan all Python files in directory tree."""
    comments_by_file: dict[str, list[dict]] = {}
    total_comments = 0

    for py_file in iter_python_files(root):
        comments = scan_file(py_file)

        if comments:
            comments_by_file[str(py_file)] = comments
            total_comments += len(comments)

    return {
        "summary": {"total_comments": total_comments, "files_with_comments": len(comments_by_file)},
        "comments": comments_by_file,
    }


def write_summary(results: dict[str, Any]) -> None:
    print(f"Total comments/docstrings: {results['summary']['total_comments']}", file=sys.stderr)
    print(f"Files with comments: {results['summary']['files_with_comments']}", file=sys.stderr)


def main() -> None:
    run_cli(scan_directory, write_summary)


if __name__ == "__main__":
    main()
