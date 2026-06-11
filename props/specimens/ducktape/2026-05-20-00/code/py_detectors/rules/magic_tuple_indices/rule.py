from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

from py_detectors.models import Detection
from py_detectors.registry import detection

DET_NAME = "magic_tuple_indices"
PROP = "avoid-magic-tuple-indices"


def _collect_indices(body: list[ast.stmt], name: str) -> set[int]:
    idx: set[int] = set()
    for n in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id == name:
            sl = n.slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, int):
                idx.add(int(sl.value))
    return idx


def find_detections(path: Path, tree: ast.AST, source: str) -> Iterable[Detection]:
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for st in fn.body or []:
            if not (isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name)):
                continue
            if not isinstance(st.value, ast.Call):
                continue
            nm = st.targets[0].id
            indices = _collect_indices(fn.body or [], nm)
            if indices and (max(indices) >= 3 or len(indices) >= 3):
                yield detection(
                    DET_NAME,
                    PROP,
                    path,
                    st.lineno,
                    st.lineno,
                    f"Multiple magic tuple indices on '{nm}': {sorted(indices)} — prefer a named model or fewer positional fields.",
                    confidence=0.8,
                )
