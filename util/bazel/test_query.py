"""Tests for util.bazel.query."""

from __future__ import annotations

from pathlib import Path
from subprocess import CalledProcessError
from unittest.mock import MagicMock, patch

import pytest
import pytest_bazel

from util.bazel.query import BazelLabel, run_query


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("//foo/bar:baz.py", BazelLabel(repo="", package=Path("foo/bar"), name="baz.py")),
        ("//:root.txt", BazelLabel(repo="", package=Path(), name="root.txt")),
        ("//a/b/c:d/e.rs", BazelLabel(repo="", package=Path("a/b/c"), name="d/e.rs")),
        ("@repo//pkg:target", BazelLabel(repo="repo", package=Path("pkg"), name="target")),
        ("@@canonical//pkg:target", BazelLabel(repo="canonical", package=Path("pkg"), name="target")),
    ],
)
def test_parse_valid(raw: str, expected: BazelLabel) -> None:
    assert BazelLabel.parse(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "//foo/bar",  # bare package ref — no colon
        "foo:bar",  # no // prefix
        "",  # empty
        "@repo",  # @ but no //
        "@@canonical",  # @@ but no //
    ],
)
def test_parse_invalid_raises(raw: str) -> None:
    with pytest.raises(ValueError, match=repr(raw) if raw else ""):
        BazelLabel.parse(raw)


@pytest.mark.parametrize("raw", ["//foo/bar", "foo:bar", ""])
def test_try_parse_invalid_returns_none(raw: str) -> None:
    assert BazelLabel.try_parse(raw) is None


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        (BazelLabel(repo="", package=Path("foo"), name="bar"), False),
        (BazelLabel(repo="some_repo", package=Path("foo"), name="bar"), True),
    ],
)
def test_is_external(label: BazelLabel, expected: bool) -> None:
    assert label.is_external is expected


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        (BazelLabel(repo="", package=Path("foo/bar"), name="baz.py"), Path("foo/bar/baz.py")),
        (BazelLabel(repo="", package=Path("foo/bar"), name="sub/qux.go"), Path("foo/bar/sub/qux.go")),
        (BazelLabel(repo="", package=Path(), name="root.txt"), Path("root.txt")),
        (
            BazelLabel(repo="", package=Path("cluster/scripts"), name="get-passwords"),
            Path("cluster/scripts/get-passwords"),
        ),
        # external → None
        (BazelLabel(repo="repo", package=Path("foo"), name="bar.py"), None),
    ],
)
def test_path_property(label: BazelLabel, expected: Path | None) -> None:
    assert label.path == expected


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        (BazelLabel(repo="", package=Path("cluster/charts/attic"), name="attic"), Path("cluster/charts/attic")),
        (BazelLabel(repo="", package=Path(), name="root"), Path()),
        (BazelLabel(repo="", package=Path("tools/orphans"), name="find_orphans"), Path("tools/orphans")),
        # external → None
        (BazelLabel(repo="repo", package=Path("foo"), name="bar"), None),
    ],
)
def test_package_path_property(label: BazelLabel, expected: Path | None) -> None:
    assert label.package_path == expected


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        (BazelLabel(repo="", package=Path("foo/bar"), name="baz.py"), "//foo/bar:baz.py"),
        (BazelLabel(repo="", package=Path(), name="root.txt"), "//:root.txt"),
        (BazelLabel(repo="repo", package=Path("pkg"), name="target"), "@repo//pkg:target"),
        (BazelLabel(repo="canonical", package=Path("pkg"), name="target"), "@canonical//pkg:target"),
    ],
)
def test_str(label: BazelLabel, expected: str) -> None:
    assert str(label) == expected


def test_run_query_parses_labels(tmp_path: Path) -> None:
    mock_result = MagicMock()
    mock_result.stdout = "//foo:bar.py\n@ext//pkg:target\n\n"
    mock_result.returncode = 0
    with patch("util.bazel.query.subprocess.run", return_value=mock_result) as mock_run:
        result = run_query("//...", cwd=tmp_path)
    (cmd,), kwargs = mock_run.call_args
    assert cmd[:3] == ["bazel", "query", "--output=label"]
    assert cmd[3].startswith("--query_file=")
    assert kwargs == {"capture_output": True, "text": True, "cwd": tmp_path, "check": False}
    assert result == [
        BazelLabel(repo="", package=Path("foo"), name="bar.py"),
        BazelLabel(repo="ext", package=Path("pkg"), name="target"),
    ]
    assert not any(isinstance(label, str) for label in result)


def test_run_query_persist_dir(tmp_path: Path) -> None:
    persist_dir = tmp_path / "persist"
    persist_dir.mkdir()
    mock_result = MagicMock()
    mock_result.stdout = "//foo:bar\n"
    mock_result.stderr = ""
    mock_result.returncode = 0
    with patch("util.bazel.query.subprocess.run", return_value=mock_result):
        run_query("//...", persist_dir=persist_dir)
    assert (persist_dir / "query").read_text() == "//..."
    assert (persist_dir / "stdout").read_text() == "//foo:bar\n"
    assert (persist_dir / "exit_code").read_text() == "0"


def test_run_query_raises_on_failure(tmp_path: Path) -> None:
    mock_result = MagicMock()
    mock_result.stdout = ""
    mock_result.stderr = "error"
    mock_result.returncode = 1
    with patch("util.bazel.query.subprocess.run", return_value=mock_result), pytest.raises(CalledProcessError):
        run_query("//...", cwd=tmp_path)


if __name__ == "__main__":
    pytest_bazel.main()
