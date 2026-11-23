import ast
from pathlib import Path

import pytest

from devinfra.lint.x.py_detectors.models import Detection
from devinfra.lint.x.py_detectors.registry import run_all


@pytest.mark.parametrize("workers", [1, 2])
def test_failure_in_one_file_keeps_other_findings(tmp_path: Path, workers: int) -> None:
    def check(path: Path, tree: ast.AST, source: str) -> list[Detection]:
        if source == "broken":
            raise ValueError("unhandled syntax")
        return [Detection(property="test", detector="check", path=path)]

    (tmp_path / "one.py").write_text("broken")
    (tmp_path / "two.py").write_text("valid")
    results = run_all({"check": check}, tmp_path, workers=workers)
    assert {result.path.name: result.message for result in results} == {
        "one.py": "detector error: unhandled syntax",
        "two.py": None,
    }
