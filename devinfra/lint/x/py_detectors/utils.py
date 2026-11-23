from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

EXCLUDES = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache"}


def iter_py_files(root: Path) -> Iterable[Path]:
    if EXCLUDES.intersection(root.parts):
        return
    for directory, subdirs, filenames in root.walk():
        subdirs[:] = [name for name in subdirs if name not in EXCLUDES]
        for name in filenames:
            if name.endswith(".py"):
                yield directory / name


def parse_python_file(path: Path) -> tuple[ast.Module, str] | None:
    """Read and parse a Python file, returning (tree, source) or None if unparseable."""
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        return tree, text
    except OSError, SyntaxError, UnicodeDecodeError:
        return None


def is_broad_exception(handler: ast.ExceptHandler) -> bool:
    """Return True for blanket except handlers (None, Exception, BaseException)."""
    if handler.type is None:
        return True
    return isinstance(handler.type, ast.Name) and handler.type.id in {"Exception", "BaseException"}
