"""Identity stability for the common pytest output fixture."""

import json
from pathlib import Path

import pytest

from devinfra.testing.output_writer import BoundTestOutputs
from devinfra.testing.pytest_outputs import case_id_for_pytest_item


def test_fixture_publishes_with_repo_relative_case_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    monkeypatch.setenv("TEST_UNDECLARED_OUTPUTS_DIR", str(tmp_path))
    outputs: BoundTestOutputs = request.getfixturevalue("test_outputs")
    payload = outputs.publish(role="diagnostics", data="example diagnostic", suffix="txt")

    root = tmp_path / "test-outputs"
    (record_path,) = (root / "records").glob("*.json")
    record = json.loads(record_path.read_text())
    assert record["case_id"] == (
        "devinfra/testing/test_test_output_pytest.py::test_fixture_publishes_with_repo_relative_case_id"
    )
    assert root / record["path"] == payload
    assert payload.read_text() == "example diagnostic"


def test_case_id_uses_repo_file_and_retains_class_and_parameters(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    first = root / "a" / "test_same.py"
    second = root / "b" / "test_same.py"
    for path in (first, second):
        path.parent.mkdir(parents=True)
        path.touch()
    assert case_id_for_pytest_item(first, "test_same.py::Case::test_same[mobile]", repo_root=root) == (
        "a/test_same.py::Case::test_same[mobile]"
    )
    assert case_id_for_pytest_item(second, "b/test_same.py::Case::test_same[mobile]", repo_root=root) == (
        "b/test_same.py::Case::test_same[mobile]"
    )
    assert case_id_for_pytest_item(first, "/unstable/root/test_same.py::Other::test_same[mobile]", repo_root=root) == (
        "a/test_same.py::Other::test_same[mobile]"
    )
    assert case_id_for_pytest_item(first, "test_same.py::Case::test_same[desktop]", repo_root=root) != (
        "a/test_same.py::Case::test_same[mobile]"
    )


def test_case_id_rejects_external_test_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside the repository"):
        case_id_for_pytest_item(tmp_path / "outside.py", "outside.py::test_example", repo_root=tmp_path / "repo")
