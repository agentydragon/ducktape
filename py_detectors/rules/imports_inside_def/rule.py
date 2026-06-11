from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from py_detectors.import_graph import (
    _resolve_from_module,
    build_import_graph,
    module_name_for_path,
    would_introduce_cycle,
)
from py_detectors.models import Detection
from py_detectors.registry import detection
from py_detectors.utils import iter_py_files, parse_python_file

DET_NAME = "imports_inside_def"
PROP = "python/imports-top"


@dataclass
class _ImportVisitorContext:
    """Context passed to ImportVisitor, avoiding captured closure variables."""

    root: Path
    path: Path
    graph: dict[str, set[str]]
    detections: list[Detection] = field(default_factory=list)


class _ImportVisitor(ast.NodeVisitor):
    """Visitor that detects imports inside function/class definitions."""

    def __init__(self, ctx: _ImportVisitorContext) -> None:
        self._ctx = ctx
        self._stack: list[ast.AST] = []

    def generic_visit(self, n: ast.AST) -> None:
        self._stack.append(n)
        try:
            super().generic_visit(n)
        finally:
            self._stack.pop()

    def visit_Import(self, n: ast.Import) -> None:
        self._maybe_report(n)
        self.generic_visit(n)

    def visit_ImportFrom(self, n: ast.ImportFrom) -> None:
        self._maybe_report(n)
        self.generic_visit(n)

    def _is_inside_type_checking_block(self) -> bool:
        """Check if we're inside an `if TYPE_CHECKING:` block."""
        for node in self._stack:
            if isinstance(node, ast.If):
                test = node.test
                if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
                    return True
                if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
                    return True
        return False

    def _maybe_report(self, n: ast.Import | ast.ImportFrom) -> None:
        if not self._stack:
            return
        if isinstance(self._stack[-1], ast.Module):
            return
        if self._is_inside_type_checking_block():
            return
        cur_mod = module_name_for_path(self._ctx.root, self._ctx.path)
        target_mod: str | None = None
        if isinstance(n, ast.Import) and n.names:
            target_mod = n.names[0].name
        elif isinstance(n, ast.ImportFrom) and (base_mod := _resolve_from_module(cur_mod, n.module, n.level)):
            if n.names:
                name0 = n.names[0].name
                target_mod = f"{base_mod}.{name0}" if name0 else base_mod
            else:
                target_mod = base_mod
        if target_mod and cur_mod and not would_introduce_cycle(self._ctx.graph, cur_mod, target_mod):
            ev = f"cur={cur_mod}, target={target_mod}, would_cycle=False"
            self._ctx.detections.append(
                detection(
                    DET_NAME,
                    PROP,
                    self._ctx.path,
                    n.lineno,
                    n.end_lineno,
                    f"Import inside function/class without cycle justification; move to module top or document a valid exception. [{ev}]",
                    confidence=0.95,
                )
            )
        elif not target_mod:
            ev = f"cur={cur_mod}, target=?"
            self._ctx.detections.append(
                detection(
                    DET_NAME,
                    PROP,
                    self._ctx.path,
                    n.lineno,
                    n.end_lineno,
                    f"Import inside function/class; target unresolved — please verify cycle/hotload justification. [{ev}]",
                    confidence=0.8,
                )
            )


def _find_in_file(path: Path, tree: ast.AST, root: Path, graph: dict[str, set[str]]) -> list[Detection]:
    ctx = _ImportVisitorContext(root=root, path=path, graph=graph)
    _ImportVisitor(ctx).visit(tree)
    return ctx.detections


def find_all(root: Path) -> list[Detection]:
    """Root-level detector that builds import graph once, then scans all files."""
    root = root.resolve()
    graph = build_import_graph(root)
    out: list[Detection] = []
    for p in iter_py_files(root):
        if parsed := parse_python_file(p):
            tree, _text = parsed
            out.extend(_find_in_file(p, tree, root, graph))
    return out
