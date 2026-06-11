from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

from py_detectors.models import Detection
from py_detectors.registry import detection

DET_NAME = "flatten_nested_guards"
PROP = "minimize-nesting"


def _simple_test(n: ast.AST) -> bool:
    if isinstance(n, ast.Name | ast.Attribute):
        return True
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not) and isinstance(n.operand, ast.Name | ast.Attribute):
        return True
    return bool(
        isinstance(n, ast.Compare)
        and isinstance(n.left, ast.Name | ast.Attribute)
        and len(n.ops) == 1
        and len(n.comparators) == 1
        and isinstance(n.comparators[0], ast.Constant | ast.Name | ast.Attribute)
    )


def find_detections(path: Path, tree: ast.AST, source: str) -> Iterable[Detection]:
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for st in fn.body:
            if not (isinstance(st, ast.If) and not st.orelse and len(st.body) == 1 and isinstance(st.body[0], ast.If)):
                continue
            inner = st.body[0]
            if not inner.orelse and _simple_test(st.test) and _simple_test(inner.test):
                yield detection(
                    DET_NAME,
                    PROP,
                    path,
                    st.lineno,
                    inner.lineno,
                    "Nested trivial guards — consider 'if A and B:' to flatten nesting",
                    confidence=0.8,
                )
