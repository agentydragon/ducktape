from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

from py_detectors.models import Detection
from py_detectors.registry import detection

DET_NAME = "import_aliasing"
PROP = "no-random-renames"

ALLOWED_IMPORT_ALIASES: set[tuple[str, str]] = {
    ("numpy", "np"),
    ("pandas", "pd"),
    ("matplotlib", "mpl"),
    ("matplotlib.pyplot", "plt"),
    ("seaborn", "sns"),
    ("networkx", "nx"),
    ("tensorflow", "tf"),
    ("sqlalchemy", "sa"),
    ("jax.numpy", "jnp"),
    ("numpy.random", "npr"),
    ("torch.nn", "nn"),
    ("torch.nn.functional", "F"),
    ("plotly.express", "px"),
}


def _collect_top_level_names(mod: ast.Module) -> set[str]:
    names: set[str] = set()
    for stmt in mod.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            names.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
    return names


def _is_allowed_alias(full: str, base: str, alias_name: str) -> bool:
    return (full, alias_name) in ALLOWED_IMPORT_ALIASES or (base, alias_name) in ALLOWED_IMPORT_ALIASES


def find_detections(path: Path, tree: ast.AST, source: str) -> Iterable[Detection]:
    if not isinstance(tree, ast.Module):
        return
    top_names = _collect_top_level_names(tree)

    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if not alias.asname:
                    continue
                full = (alias.name or "").strip()
                base = full.split(".")[0] if full else ""
                if (
                    base
                    and alias.asname != base
                    and base not in top_names
                    and not _is_allowed_alias(full, base, alias.asname)
                ):
                    yield detection(
                        DET_NAME,
                        PROP,
                        path,
                        stmt.lineno,
                        stmt.lineno,
                        f"Import alias without local collision: 'import {alias.name} as {alias.asname}' — prefer direct name unless disambiguation/collision.",
                        confidence=0.7,
                    )
        elif isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                if not (alias.asname and alias.name):
                    continue
                full_mod = stmt.module or ""
                allowed = (full_mod, alias.name) == ("torch.nn", "functional") and alias.asname == "F"
                if alias.name != alias.asname and alias.name not in top_names and not allowed:
                    yield detection(
                        DET_NAME,
                        PROP,
                        path,
                        stmt.lineno,
                        stmt.lineno,
                        f"Import alias without local collision: 'from {full_mod} import {alias.name} as {alias.asname}' — prefer direct name unless disambiguation/collision.",
                        confidence=0.7,
                    )
