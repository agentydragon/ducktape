from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

from py_detectors.base import BaseDetector
from py_detectors.models import Detection


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
    """Extract names used in a function call's arguments and total arg count.

    Returns (name_args, total_args) where name_args is the set of argument names
    that are simple Name nodes (i.e., just forwarding a variable).
    """
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
    """Check if function is a trivial passthrough.

    Returns (is_trivial, called_name) where called_name is the function/class being called.
    """
    body = fn.body

    # Skip docstring if present
    start_idx = 0
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        start_idx = 1

    # Must have exactly one statement after optional docstring
    if len(body) - start_idx != 1:
        return False, None

    stmt = body[start_idx]

    # Must be a return statement
    if not isinstance(stmt, ast.Return) or stmt.value is None:
        return False, None

    # The return value must be a call
    if not isinstance(stmt.value, ast.Call):
        return False, None

    call = stmt.value

    # Get the called function/class name
    if isinstance(call.func, ast.Name):
        called_name = call.func.id
    elif isinstance(call.func, ast.Attribute):
        called_name = call.func.attr
    else:
        return False, None

    # Get parameter names and call argument info
    param_names = _get_param_names(fn.args)
    call_arg_names, call_arg_count = _get_call_arg_info(call)

    # Trivial if:
    # 1. All call args are simple Name nodes (just forwarding params)
    # 2. All call args come from function params
    # 3. The number of call args matches number of params (no extra defaults added)
    if not param_names:
        # No params but still just calls something - could be trivial if no args passed
        if call_arg_count == 0:
            return True, called_name
        return False, None

    # All call argument names must be subset of params (no external variables)
    if not call_arg_names.issubset(param_names):
        return False, None

    # Number of call args should match number of name-based call args
    # (all args should be simple Name nodes, no literals/expressions)
    if len(call_arg_names) != call_arg_count:
        return False, None

    # Most params should be forwarded (allow some tolerance for self, cls, etc.)
    forwarded = param_names & call_arg_names
    if len(forwarded) >= len(param_names) * 0.8:
        return True, called_name

    return False, None


class TrivialPassthroughDetector(BaseDetector):
    DET_NAME = "trivial_passthrough"
    PROP = "no-oneoff-vars-and-trivial-wrappers"

    def find_detections(self, path: Path, tree: ast.AST, source: str) -> Iterable[Detection]:
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue

            is_trivial, called_name = _is_trivial_passthrough(node)
            if is_trivial and called_name:
                yield self.detection(
                    path,
                    node.lineno,
                    node.end_lineno,
                    f"Trivial passthrough '{node.name}' just calls '{called_name}'; consider using '{called_name}' directly.",
                    confidence=0.85,
                )


_detector = TrivialPassthroughDetector()
find = _detector.get_finder()
_detector.register_detector()
