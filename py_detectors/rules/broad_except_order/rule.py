from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

from py_detectors.models import Detection
from py_detectors.registry import detection
from py_detectors.utils import is_broad_exception

DET_NAME = "broad_except_order"
PROP = "python/scoped-try-except"


def find_detections(path: Path, tree: ast.AST, source: str) -> Iterable[Detection]:
    for n in ast.walk(tree):
        if not isinstance(n, ast.Try):
            continue
        handlers = n.handlers or []
        if (first_broad := next((i for i, h in enumerate(handlers) if is_broad_exception(h)), None)) is None:
            continue
        if any(not is_broad_exception(h) for h in handlers[first_broad + 1 :]):
            broad = handlers[first_broad]
            yield detection(
                DET_NAME,
                PROP,
                path,
                broad.lineno,
                broad.end_lineno,
                "Broad except precedes specific handler; later handler is unreachable.",
                confidence=0.95,
            )
