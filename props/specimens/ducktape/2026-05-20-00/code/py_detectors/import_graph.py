from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModuleInfo:
    path: Path
    name: str


def module_name_for_path(root: Path, file_path: Path) -> str:
    rel = file_path.resolve().relative_to(root.resolve())
    if rel.suffix == ".py":
        rel = rel.with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_from_module(base: str, module: str | None, level: int) -> str | None:
    """Resolve the imported module name for an ImportFrom.

    Rules:
    - Absolute import (level == 0 and module provided): return module as-is.
    - Relative import (level > 0): truncate base by `level` and then append module (if any).
    - Bare relative (e.g., `from . import x`): return the truncated base.
    """
    base_parts = base.split(".") if base else []
    if level == 0 and module:
        return module
    if level > 0:
        if len(base_parts) < level:
            return None
        base_parts = base_parts[: len(base_parts) - level]
    target_parts = module.split(".") if module else []
    parts = base_parts + target_parts
    return ".".join(p for p in parts if p)


def build_import_graph(root: Path) -> dict[str, set[str]]:
    """Build a directed graph of top-level module imports.

    Edge m -> n means module m has a top-level import of module n (or its submodule).
    """
    graph: dict[str, set[str]] = {}
    for file_path in root.rglob("*.py"):
        if any(part in {".git", "__pycache__", ".venv", "venv"} for part in file_path.parts):
            continue
        try:
            text = file_path.read_text(encoding="utf-8")
            node = ast.parse(text)
        except (SyntaxError, UnicodeDecodeError):
            # Skip files with invalid Python syntax or encoding issues
            continue
        mod = module_name_for_path(root, file_path)
        if not mod:
            continue
        graph.setdefault(mod, set())
        for stmt in node.body:
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    name = alias.name  # e.g., pkg.sub
                    graph[mod].add(name)
            elif isinstance(stmt, ast.ImportFrom):
                target = _resolve_from_module(mod, stmt.module, stmt.level)
                if target:
                    graph[mod].add(target)
    return graph


def _has_path(graph: dict[str, set[str]], src: str, dst: str, *, max_visits: int = 10000) -> bool:
    if src == dst:
        return True
    seen: set[str] = set()
    stack: list[str] = [src]
    visits = 0
    while stack:
        if visits > max_visits:
            break
        visits += 1
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for nxt in graph.get(cur, ()):  # neighbors
            if nxt == dst:
                return True
            if nxt not in seen:
                stack.append(nxt)
    return False


def would_introduce_cycle(graph: dict[str, set[str]], a: str, b: str) -> bool:
    """Return True if adding edge a->b would create a cycle (i.e., b reaches a)."""
    return _has_path(graph, b, a)
