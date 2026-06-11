"""Generic per-rule test runner. Each py_detector() test target runs this with DETECTOR_NAME env var."""

from __future__ import annotations

import importlib
import os
import shutil
from pathlib import Path

import pytest
import pytest_bazel

from py_detectors.registry import run_all

DETECTOR_NAME = os.environ["DETECTOR_NAME"]
FIXTURES_DIR = Path(__file__).parent / DETECTOR_NAME

_SKIP = {"__pycache__", "__init__.py"}


def _cases(kind: str) -> list[Path]:
    base = FIXTURES_DIR / kind
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.name not in _SKIP)


_MOD = importlib.import_module(f"py_detectors.rules.{DETECTOR_NAME}.rule")

# Per-file detectors export find_detections; root-level detectors export find_all.
_IS_ROOT_DETECTOR = hasattr(_MOD, "find_all") and not hasattr(_MOD, "find_detections")


def _run(tmp_path: Path, fixture: Path):
    root = tmp_path / "repo"
    dest = root / "pkg" / fixture.name
    if fixture.is_dir():
        shutil.copytree(fixture, dest)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixture, dest)
    if _IS_ROOT_DETECTOR:
        return _MOD.find_all(root)
    return run_all({DETECTOR_NAME: _MOD.find_detections}, root)


BAD_CASES = _cases("bad")
OK_CASES = _cases("ok")


@pytest.mark.parametrize("fixture", BAD_CASES, ids=lambda p: p.name)
def test_bad(tmp_path: Path, fixture: Path):
    assert _run(tmp_path, fixture), f"no detections on {fixture.name}"


@pytest.mark.parametrize("fixture", OK_CASES, ids=lambda p: p.name)
def test_ok(tmp_path: Path, fixture: Path):
    dets = _run(tmp_path, fixture)
    assert not dets, f"unexpected: {dets}"


if __name__ == "__main__":
    pytest_bazel.main()
