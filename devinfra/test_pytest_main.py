"""Tests for the Bazel pytest entrypoint checker."""

from pathlib import Path

import pytest_bazel

from devinfra.pytest_main import BazelPyTestIndex, check_file


def test_rejects_py_test_source_without_entrypoint(tmp_path: Path) -> None:
    test_file = Path("test_missing.py")
    (tmp_path / test_file).write_text("def test_example():\n    assert True\n")
    index = BazelPyTestIndex(known_srcs={(tmp_path / test_file).resolve()})

    result = check_file(test_file, tmp_path, index)

    assert result.passed is False
    assert result.reason == "missing pytest_bazel.main() entry point"


def test_allows_py_test_source_with_entrypoint(tmp_path: Path) -> None:
    test_file = Path("test_configured.py")
    (tmp_path / test_file).write_text('if __name__ == "__main__":\n    pytest_bazel.main()\n')
    index = BazelPyTestIndex(known_srcs={(tmp_path / test_file).resolve()})

    result = check_file(test_file, tmp_path, index)

    assert result.passed is True
    assert result.reason == "has pytest_bazel.main()"


def test_allows_py_test_source_with_custom_bazel_main(tmp_path: Path) -> None:
    test_file = Path("test_custom_main.py")
    source = (tmp_path / test_file).resolve()
    source.write_text("def test_example():\n    assert True\n")
    index = BazelPyTestIndex(known_srcs={source}, exempt_srcs={source})

    result = check_file(test_file, tmp_path, index)

    assert result.passed is True
    assert result.reason == "exempt: py_test uses custom main= (bazel query)"


if __name__ == "__main__":
    pytest_bazel.main()
