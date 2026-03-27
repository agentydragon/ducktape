from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest
import pytest_bazel

# Ensure detectors register themselves via module imports
import py_detectors.main  # noqa: F401
from py_detectors.registry import all_detectors, run_all
from tests.fixture_utils import copy_fixture, iter_children


def _discover_fixtures(base_package: str) -> list[tuple[str, str]]:
    """Discover (fixture_path, detector_name) from directory structure.

    Expects fixtures organized as: base_package/<detector_name>/<case>.py
    """
    base = resources.files(base_package)
    cases: list[tuple[str, str]] = []
    for detector_dir in iter_children(base):
        if not detector_dir.is_dir():
            continue
        detector_name = detector_dir.name
        cases.extend((f"{detector_name}/{item.name}", detector_name) for item in iter_children(detector_dir))
    return sorted(cases)


def _get_expected_property(detector_name: str) -> str:
    """Look up the target_property for a detector from the registry."""
    for spec in all_detectors():
        if spec.name == detector_name:
            return spec.target_property
    raise ValueError(f"Unknown detector: {detector_name}")


BAD_CASES = _discover_fixtures("tests.fixtures.bad")
OK_CASES = _discover_fixtures("tests.fixtures.ok")


def _run_case(tmp_path: Path, pkg_base: str, fixture_file: str, detector: str):
    root = tmp_path / "repo"
    dest_rel = f"pkg/{fixture_file}"
    copy_fixture(root, pkg_base, fixture_file, dest_rel)
    return run_all(root, detector_names=[detector])


@pytest.mark.parametrize(("fixture_file", "detector"), BAD_CASES)
def test_detectors_bad(tmp_path: Path, fixture_file: str, detector: str):
    expected_property = _get_expected_property(detector)
    detections = _run_case(tmp_path, "tests.fixtures.bad", fixture_file, detector)
    assert detections, f"no detections for {detector}"
    assert any(d.property == expected_property for d in detections)


@pytest.mark.parametrize(("fixture_file", "detector"), OK_CASES)
def test_detectors_ok(tmp_path: Path, fixture_file: str, detector: str):
    detections = _run_case(tmp_path, "tests.fixtures.ok", fixture_file, detector)
    assert not detections, f"unexpected detections for {detector}: {detections}"


if __name__ == "__main__":
    pytest_bazel.main()
