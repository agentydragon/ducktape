from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

from py_detectors.models import Detection
from py_detectors.registry import detection

DET_NAME = "trivial_passthrough"
PROP = "no-oneoff-vars-and-trivial-wrappers"


def _get_param_names(args: ast.arguments) -> set[str]:
    """Extract all parameter names from function signature."""
    names: set[str] = set()
    for arg in args.posonlyargs + args.args + args.kwonlyargs:
        names.add(arg.arg)
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _get_call_arg_info(call: ast.Call) -> tuple[set[str], int]:
    """Extract names used in a function call's arguments and total arg count."""
    names: set[str] = set()
    total = 0
    for arg in call.args:
        total += 1
        if isinstance(arg, ast.Starred) and isinstance(arg.value, ast.Name):
            names.add(arg.value.id)
        elif isinstance(arg, ast.Name):
            names.add(arg.id)
    for kw in call.keywords:
        total += 1
        if isinstance(kw.value, ast.Name):
            names.add(kw.value.id)
    return names, total


def _is_trivial_passthrough(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[bool, str | None]:
    """Check if function is a trivial passthrough."""
    body = fn.body

    # Skip docstring if present
    start_idx = 0
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        start_idx = 1

    if len(body) - start_idx != 1:
        return False, None

    stmt = body[start_idx]

    if not isinstance(stmt, ast.Return) or stmt.value is None:
        return False, None

    if not isinstance(stmt.value, ast.Call):
        return False, None

    call = stmt.value

    if isinstance(call.func, ast.Name):
        called_name = call.func.id
    elif isinstance(call.func, ast.Attribute):
        called_name = call.func.attr
    else:
        return False, None

    param_names = _get_param_names(fn.args)
    call_arg_names, call_arg_count = _get_call_arg_info(call)

    if not param_names:
        if call_arg_count == 0:
            return True, called_name
        return False, None

    if not call_arg_names.issubset(param_names):
        return False, None

    if len(call_arg_names) != call_arg_count:
        return False, None

    forwarded = param_names & call_arg_names
    if len(forwarded) >= len(param_names) * 0.8:
        return True, called_name

    return False, None


def find_detections(path: Path, tree: ast.AST, source: str) -> Iterable[Detection]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue

        is_trivial, called_name = _is_trivial_passthrough(node)
        if is_trivial and called_name:
            yield detection(
                DET_NAME,
                PROP,
                path,
                node.lineno,
                node.end_lineno,
                f"Trivial passthrough '{node.name}' just calls '{called_name}'; consider using '{called_name}' directly.",
                confidence=0.85,
            )
