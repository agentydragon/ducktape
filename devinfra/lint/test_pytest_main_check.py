"""Unit tests for the pytest-main aspect's checker action."""

from pathlib import Path

import pytest_bazel

from devinfra.lint.pytest_main_check import check_sources


def test_rejects_source_without_entrypoint(tmp_path: Path) -> None:
    source = tmp_path / "test_missing.py"
    source.write_text("def test_example():\n    assert True\n")
    assert check_sources([source]) == [f"{source}: missing pytest_bazel.main() entry point"]


def test_accepts_both_entrypoint_markers(tmp_path: Path) -> None:
    bazel_source = tmp_path / "test_bazel.py"
    bazel_source.write_text('if __name__ == "__main__":\n    pytest_bazel.main()\n')
    pytest_source = tmp_path / "test_pytest.py"
    pytest_source.write_text("pytest.main(['tests'])\n")
    assert check_sources([bazel_source, pytest_source]) == []


if __name__ == "__main__":
    pytest_bazel.main()
