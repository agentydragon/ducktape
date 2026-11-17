"""Shared traversal and CLI handling for the code scanners."""

import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

EXCLUDED_DIRECTORY_PARTS = frozenset({"venv", "__pycache__", ".git", "node_modules", ".tox"})


def iter_python_files(root: Path) -> Iterator[Path]:
    for py_file in root.rglob("*.py"):
        if EXCLUDED_DIRECTORY_PARTS.isdisjoint(py_file.parts):
            yield py_file


def run_cli(scan_directory: Callable[[Path], dict[str, Any]], write_summary: Callable[[dict[str, Any]], None]) -> None:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()

    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning {root}...", file=sys.stderr)
    results = scan_directory(root)
    print(json.dumps(results, indent=2))

    print("\n=== Summary ===", file=sys.stderr)
    write_summary(results)
