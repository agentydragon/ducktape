"""Identity stability for the common pytest output fixture."""

import json
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.testing.output_writer import BoundTestOutputs
from devinfra.testing.pytest_outputs import case_id_for_pytest_item


def test_bound_fixture_uses_repository_relative_node_id(test_outputs: BoundTestOutputs) -> None:
    assert test_outputs.case_id == (
        "devinfra/testing/test_test_output_pytest.py::test_bound_fixture_uses_repository_relative_node_id"
    )


def test_bound_writer_can_publish_an_existing_file(tmp_path: Path) -> None:
    source = tmp_path / "image.png"
    source.write_bytes(b"image")
    outputs = BoundTestOutputs(tmp_path / "outputs", "example/test.py::test_case")
    payload = outputs.publish_file(source=source, role="screenshot")
    record = json.loads(next((outputs.output_dir / "test-outputs" / "records").glob("*.json")).read_text())
    assert payload.read_bytes() == b"image"
    assert record["case_id"] == outputs.case_id
    assert record["checkpoint"] is None


def test_bound_writer_accepts_text_and_file_style_output(tmp_path: Path) -> None:
    outputs = BoundTestOutputs(tmp_path / "outputs", "example/test.py::test_case")
    payload = outputs.publish(role="diagnostics", checkpoint="first", data="猫", suffix="txt")
    assert payload.read_bytes() == "猫".encode()

    with outputs.open(role="diagnostics", checkpoint="second", suffix="json") as output:
        json.dump({"ready": True}, output)
    records = list((outputs.output_dir / "test-outputs" / "records").glob("*.json"))
    assert len(records) == 2
    second = next(
        json.loads(path.read_text()) for path in records if json.loads(path.read_text())["checkpoint"] == "second"
    )
    assert json.loads((outputs.output_dir / "test-outputs" / second["path"]).read_text()) == {"ready": True}


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


if __name__ == "__main__":
    pytest_bazel.main()
