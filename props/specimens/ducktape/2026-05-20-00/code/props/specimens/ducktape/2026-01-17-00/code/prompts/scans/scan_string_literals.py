#!/usr/bin/env python3
"""Scan Python codebase for string literals and symbol names to detect stringly-typed patterns.

This script extracts:
1. ALL string literals (< 50 chars) with frequency histogram
2. ALL symbol names (classes, functions, variables, fields)
3. Cross-references to identify overlaps (string literals matching symbol names)

The histogram is sorted by frequency (most common first) and includes file locations
for each occurrence.

Usage:
    python scan_string_literals.py [directory]

Output:
    JSON with:
    - literal_histogram: {literal: {count, locations: [{file, line}]}}
    - symbol_histogram: {symbol: {count, locations: [{file, line, type}]}}
    - overlaps: literals that match symbol names (strong stringly-typed indicator)
    - summary: counts and statistics
"""

import ast
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


class LiteralAndSymbolExtractor(ast.NodeVisitor):
    """Extract string literals and symbol names from AST."""

    def __init__(self, filepath: Path):
        self.filepath = filepath
        self.literals: list[dict[str, Any]] = []
        self.symbols: list[dict[str, Any]] = []

    def visit_Constant(self, node: ast.Constant) -> None:
        """Extract string literals."""
        if isinstance(node.value, str):
            literal = node.value
            # Filter: only strings < 50 chars, skip empty strings
            if 0 < len(literal) < 50:
                self.literals.append({"value": literal, "line": node.lineno, "file": str(self.filepath)})
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Extract class names."""
        self.symbols.append({"name": node.name, "line": node.lineno, "file": str(self.filepath), "type": "class"})
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Extract function names."""
        self.symbols.append({"name": node.name, "line": node.lineno, "file": str(self.filepath), "type": "function"})
        # Extract parameter names
        for arg in node.args.args:
            self.symbols.append({"name": arg.arg, "line": node.lineno, "file": str(self.filepath), "type": "parameter"})
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Extract async function names."""
        self.symbols.append(
            {"name": node.name, "line": node.lineno, "file": str(self.filepath), "type": "async_function"}
        )
        # Extract parameter names
        for arg in node.args.args:
            self.symbols.append({"name": arg.arg, "line": node.lineno, "file": str(self.filepath), "type": "parameter"})
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        """Extract variable names (in assignments)."""
        # Only extract when it's being assigned to (Store context)
        if isinstance(node.ctx, ast.Store):
            self.symbols.append({"name": node.id, "line": node.lineno, "file": str(self.filepath), "type": "variable"})
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Extract annotated field/variable names."""
        if isinstance(node.target, ast.Name):
            self.symbols.append(
                {"name": node.target.id, "line": node.lineno, "file": str(self.filepath), "type": "field"}
            )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Extract attribute names."""
        self.symbols.append({"name": node.attr, "line": node.lineno, "file": str(self.filepath), "type": "attribute"})
        self.generic_visit(node)


def scan_file(filepath: Path) -> tuple[list[dict], list[dict]]:
    """Scan a single Python file for literals and symbols.

    Returns:
        (literals, symbols)
    """
    try:
        content = filepath.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(filepath))
        extractor = LiteralAndSymbolExtractor(filepath)
        extractor.visit(tree)
        return extractor.literals, extractor.symbols
    except (SyntaxError, UnicodeDecodeError):
        return [], []


def build_histogram(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    """Build histogram from items grouped by key.

    Returns:
        {value: {count, locations: [{file, line, ...}]}}
    """
    histogram: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        value = item[key]
        histogram[value].append(item)

    # Sort by frequency (most common first)
    sorted_histogram = dict(sorted(histogram.items(), key=lambda x: len(x[1]), reverse=True))

    # Format output
    result = {}
    for value, occurrences in sorted_histogram.items():
        result[value] = {
            "count": len(occurrences),
            "locations": [{k: v for k, v in occ.items() if k != key} for occ in occurrences],
        }

    return result


def find_overlaps(literal_histogram: dict[str, dict], symbol_histogram: dict[str, dict]) -> dict[str, dict]:
    """Find string literals that match symbol names.

    These are strong indicators of stringly-typed patterns - when you have
    both a symbol named 'status' and string literals "status" appearing in code.
    """
    overlaps = {}
    for literal in literal_histogram:
        if literal in symbol_histogram:
            overlaps[literal] = {
                "literal_count": literal_histogram[literal]["count"],
                "symbol_count": symbol_histogram[literal]["count"],
                "literal_locations": literal_histogram[literal]["locations"],
                "symbol_locations": symbol_histogram[literal]["locations"],
            }
    return dict(sorted(overlaps.items(), key=lambda x: x[1]["literal_count"], reverse=True))


def scan_directory(directory: Path) -> dict[str, Any]:
    """Scan all Python files in directory."""
    all_literals = []
    all_symbols = []

    for filepath in directory.rglob("*.py"):
        # Skip venv, .git, etc.
        if any(part.startswith(".") or part == "venv" for part in filepath.parts):
            continue

        literals, symbols = scan_file(filepath)
        all_literals.extend(literals)
        all_symbols.extend(symbols)

    # Build histograms
    literal_histogram = build_histogram(all_literals, "value")
    symbol_histogram = build_histogram(all_symbols, "name")

    # Find overlaps
    overlaps = find_overlaps(literal_histogram, symbol_histogram)

    return {
        "summary": {
            "total_literals": len(all_literals),
            "unique_literals": len(literal_histogram),
            "total_symbols": len(all_symbols),
            "unique_symbols": len(symbol_histogram),
            "overlaps": len(overlaps),
        },
        "literal_histogram": literal_histogram,
        "symbol_histogram": symbol_histogram,
        "overlaps": overlaps,
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
