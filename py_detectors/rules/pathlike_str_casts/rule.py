from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

from py_detectors.models import Detection
from py_detectors.registry import detection

DET_NAME = "pathlike_str_casts"
PROP = "python/pathlike"

KNOWN_FUNCS = {
    ("subprocess", "run"),
    ("subprocess", "check_call"),
    ("subprocess", "check_output"),
    ("subprocess", "Popen"),
    ("shutil", "copy"),
    ("shutil", "copyfile"),
    ("logging", "FileHandler"),
    ("zipfile", "ZipFile"),
    ("tarfile", "open"),
}


def _is_pathlike_api(func: ast.expr) -> bool:
    """Check if func is a known PathLike-accepting API."""
    if isinstance(func, ast.Name) and func.id == "open":
        return True
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return (func.value.id, func.attr) in KNOWN_FUNCS
    return False


def _has_str_call(node: ast.AST) -> bool:
    """Check if any subtree contains str(...)."""
    return any(
        isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "str" for sub in ast.walk(node)
    )


def find_detections(path: Path, tree: ast.AST, source: str) -> Iterable[Detection]:
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and _is_pathlike_api(n.func)):
            continue
        if any(_has_str_call(a) for a in [*n.args, *(kw.value for kw in n.keywords)]):
            yield detection(
                DET_NAME,
                PROP,
                path,
                n.lineno,
                n.end_lineno,
                "Casting PathLike to str for a PathLike-accepting API; pass Path directly.",
            )
