from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

from py_detectors.models import Detection
from py_detectors.registry import detection

DET_NAME = "trivial_alias"
PROP = "no-oneoff-vars-and-trivial-wrappers"


class _UseCounter(ast.NodeVisitor):
    def __init__(self, target: str) -> None:
        self.target = target
        self.loads = 0
        self.stores = 0

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == self.target:
            if isinstance(node.ctx, ast.Load):
                self.loads += 1
            elif isinstance(node.ctx, ast.Store):
                self.stores += 1
        self.generic_visit(node)


def _count_uses_in_body(body: list[ast.stmt], start_index: int, name: str) -> tuple[int, int]:
    c = _UseCounter(name)
    c.visit(ast.Module(body=body[start_index + 1 :], type_ignores=[]))
    return c.loads, c.stores


def find_detections(path: Path, tree: ast.AST, source: str) -> Iterable[Detection]:
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for i, st in enumerate(fn.body):
            if not (
                isinstance(st, ast.Assign)
                and len(st.targets) == 1
                and isinstance(st.targets[0], ast.Name)
                and isinstance(st.value, ast.Name)
            ):
                continue
            lhs, rhs = st.targets[0].id, st.value.id
            lhs_loads, _ = _count_uses_in_body(fn.body, i, lhs)
            _, rhs_stores = _count_uses_in_body(fn.body, i, rhs)
            if lhs_loads == 1 and rhs_stores == 0:
                yield detection(
                    DET_NAME,
                    PROP,
                    path,
                    st.lineno,
                    st.lineno,
                    f"Trivial alias '{lhs} = {rhs}' used once; consider inlining '{rhs}' at use site.",
                    confidence=0.85,
                )
