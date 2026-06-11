from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

from py_detectors.models import Detection
from py_detectors.registry import detection

DET_NAME = "walrus_suggest"
PROP = "python/walrus"


def _is_simple_guard(test: ast.AST, name: str) -> str | None:
    """Returns a short description when test uses only the given name in simple ways."""
    if isinstance(test, ast.Name) and test.id == name:
        return "truthiness"
    if (
        isinstance(test, ast.UnaryOp)
        and isinstance(test.op, ast.Not)
        and isinstance(test.operand, ast.Name)
        and test.operand.id == name
    ):
        return "not name"
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and len(test.comparators) == 1):
        return None
    left, op, right = test.left, test.ops[0], test.comparators[0]
    if not (isinstance(left, ast.Name) and left.id == name):
        return None
    if isinstance(op, ast.Is | ast.IsNot) and isinstance(right, ast.Constant) and right.value is None:
        return "is None" if isinstance(op, ast.Is) else "is not None"
    if isinstance(op, ast.Eq | ast.NotEq) and isinstance(right, ast.Constant | ast.Str | ast.Num):
        return "== literal" if isinstance(op, ast.Eq) else "!= literal"
    return None


def find_detections(path: Path, tree: ast.AST, source: str) -> Iterable[Detection]:
    for parent in ast.walk(tree):
        if not isinstance(parent, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = parent.body
        for i, stmt in enumerate(body[:-1]):
            if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)):
                continue
            name = stmt.targets[0].id
            next_stmt = body[i + 1]
            # Skip standalone string doc/comment statements
            if isinstance(next_stmt, ast.Expr) and isinstance(next_stmt.value, ast.Str):
                if i + 2 >= len(body):
                    continue
                next_stmt = body[i + 2]
            if not isinstance(next_stmt, ast.If | ast.While):
                continue
            if desc := _is_simple_guard(next_stmt.test, name):
                yield detection(
                    DET_NAME,
                    PROP,
                    path,
                    stmt.lineno,
                    next_stmt.lineno,
                    f"Assign then immediate guard on '{name}' — consider walrus in guard (assign L{stmt.lineno}, guard L{next_stmt.lineno}, test={desc}).",
                    confidence=0.8,
                )
