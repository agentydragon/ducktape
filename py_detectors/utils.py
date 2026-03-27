from __future__ import annotations

import ast
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from py_detectors.models import Detection

EXCLUDES = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache"}


def iter_py_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*.py"):
        parts = set(p.parts)
        if parts & EXCLUDES:
            continue
        yield p


def run_file_detector(root: Path, finder: Callable[[Path], list[Detection]]) -> list[Detection]:
    """Apply a per-file detector across the repository Python files."""
    detections: list[Detection] = []
    for path in iter_py_files(root):
        detections.extend(finder(path))
    return detections


def make_root_detector(finder: Callable[[Path], list[Detection]]) -> Callable[[Path], list[Detection]]:
    """Wrap a per-file finder into a repository-level detector."""

    def _run(root: Path) -> list[Detection]:
        return run_file_detector(root, finder)

    return _run


def parse_python_file(path: Path) -> tuple[ast.AST, str] | None:
    """Read and parse a Python file, returning (tree, source) or None if unparseable."""
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        return tree, text
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


def is_broad_exception(handler: ast.ExceptHandler) -> bool:
    """Return True for blanket except handlers (None, Exception, BaseException)."""
    if handler.type is None:
        return True
    return isinstance(handler.type, ast.Name) and handler.type.id in {"Exception", "BaseException"}


def read_snippet(path: Path, start: int, end: int | None, context: int = 0) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # File unreadable - return empty snippet
        return ""
    lines = text.splitlines()
    s = max(1, start - context)
    e = min(len(lines), (end or start) + context)
    # 1-based indexing for display
    out = [f"{i:>5}: {lines[i - 1]}" for i in range(s, e + 1)]
    return "\n".join(out)
