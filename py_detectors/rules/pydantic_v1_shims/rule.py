from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

from py_detectors.models import Detection
from py_detectors.registry import detection

DET_NAME = "pydantic_v1_shims"
PROP = "python/pydantic-2"


def find_detections(path: Path, tree: ast.AST, source: str) -> Iterable[Detection]:
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("pydantic.v1"):
            yield detection(
                DET_NAME,
                PROP,
                path,
                n.lineno,
                n.lineno,
                "Import from pydantic.v1 — target Pydantic 2 APIs only.",
                confidence=0.95,
            )
        elif isinstance(n, ast.Import):
            for alias in n.names:
                if alias.name == "pydantic.v1":
                    yield detection(
                        DET_NAME,
                        PROP,
                        path,
                        n.lineno,
                        n.lineno,
                        "Importing pydantic.v1 — target Pydantic 2 APIs only.",
                        confidence=0.95,
                    )
        elif isinstance(n, ast.ClassDef):
            for inner in n.body:
                if isinstance(inner, ast.ClassDef) and inner.name == "Config":
                    yield detection(
                        DET_NAME,
                        PROP,
                        path,
                        inner.lineno,
                        inner.end_lineno,
                        "Pydantic v1 style `class Config:` — use model_config = ConfigDict(...).",
                    )
        elif isinstance(n, ast.FunctionDef):
            for dec in n.decorator_list:
                if isinstance(dec, ast.Name) and dec.id in {"validator", "root_validator"}:
                    yield detection(
                        DET_NAME,
                        PROP,
                        path,
                        dec.lineno,
                        dec.lineno,
                        "Pydantic v1 validator decorator — use field_validator/model_validator in v2.",
                    )
