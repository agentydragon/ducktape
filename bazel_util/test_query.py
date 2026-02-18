"""Tests for bazel_util.query."""

from __future__ import annotations

from pathlib import Path
from subprocess import CalledProcessError
from unittest.mock import MagicMock, patch

import pytest
import pytest_bazel

from bazel_util.query import BazelLabel, label_to_package, label_to_path, run_query

# ---------------------------------------------------------------------------
# BazelLabel.parse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("//foo/bar:baz.py", BazelLabel(repo="", package=Path("foo/bar"), name=Path("baz.py"))),
        ("//:root.txt", BazelLabel(repo="", package=Path(), name=Path("root.txt"))),
        ("//a/b/c:d/e.rs", BazelLabel(repo="", package=Path("a/b/c"), name=Path("d/e.rs"))),
        ("@repo//pkg:target", BazelLabel(repo="repo", package=Path("pkg"), name=Path("target"))),
        ("@@canonical//pkg:target", BazelLabel(repo="canonical", package=Path("pkg"), name=Path("target"))),
        # bare package ref — no colon
        ("//foo/bar", None),
        # no // prefix
        ("foo:bar", None),
        ("", None),
    ],
)
def test_parse(raw: str, expected: BazelLabel | None) -> None:
    assert BazelLabel.parse(raw) == expected


# ---------------------------------------------------------------------------
# BazelLabel.is_external
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        (BazelLabel(repo="", package=Path("foo"), name=Path("bar")), False),
        (BazelLabel(repo="some_repo", package=Path("foo"), name=Path("bar")), True),
    ],
)
def test_is_external(label: BazelLabel, expected: bool) -> None:
    assert label.is_external is expected


# ---------------------------------------------------------------------------
# BazelLabel equality / hash / not-a-str
# ---------------------------------------------------------------------------


def test_equality_and_hash() -> None:
    a = BazelLabel(repo="", package=Path("foo"), name=Path("bar"))
    b = BazelLabel(repo="", package=Path("foo"), name=Path("bar"))
    c = BazelLabel(repo="", package=Path("foo"), name=Path("baz"))
    assert a == b
    assert a != c
    assert hash(a) == hash(b)
    assert {a, b} == {BazelLabel(repo="", package=Path("foo"), name=Path("bar"))}


def test_not_a_str() -> None:
    assert not isinstance(BazelLabel(repo="", package=Path("foo"), name=Path("bar.py")), str)


# ---------------------------------------------------------------------------
# label_to_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        (BazelLabel(repo="", package=Path("foo/bar"), name=Path("baz.py")), Path("foo/bar/baz.py")),
        (BazelLabel(repo="", package=Path("foo/bar"), name=Path("sub/qux.go")), Path("foo/bar/sub/qux.go")),
        (BazelLabel(repo="", package=Path(), name=Path("root.txt")), Path("root.txt")),
        (
            BazelLabel(repo="", package=Path("cluster/scripts"), name=Path("get-passwords")),
            Path("cluster/scripts/get-passwords"),
        ),
        # external → None
        (BazelLabel(repo="repo", package=Path("foo"), name=Path("bar.py")), None),
    ],
)
def test_label_to_path(label: BazelLabel, expected: Path | None) -> None:
    assert label_to_path(label) == expected


# ---------------------------------------------------------------------------
# label_to_package
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        (BazelLabel(repo="", package=Path("cluster/charts/attic"), name=Path("attic")), Path("cluster/charts/attic")),
        (BazelLabel(repo="", package=Path(), name=Path("root")), Path()),
        (BazelLabel(repo="", package=Path("tools/orphans"), name=Path("find_orphans")), Path("tools/orphans")),
        # external → None
        (BazelLabel(repo="repo", package=Path("foo"), name=Path("bar")), None),
    ],
)
def test_label_to_package(label: BazelLabel, expected: Path | None) -> None:
    assert label_to_package(label) == expected


# ---------------------------------------------------------------------------
# run_query
# ---------------------------------------------------------------------------


def test_run_query_parses_labels(tmp_path: Path) -> None:
    mock_result = MagicMock()
    mock_result.stdout = "//foo:bar.py\n@ext//pkg:target\n\n"
    with patch("bazel_util.query.subprocess.run", return_value=mock_result) as mock_run:
        result = run_query("//...", cwd=tmp_path)
    (cmd,), kwargs = mock_run.call_args
    assert cmd[:2] == ["bazel", "query"]
    assert cmd[2].startswith("--query_file=")
    assert kwargs == {"capture_output": True, "text": True, "cwd": tmp_path, "check": True}
    assert result == [
        BazelLabel(repo="", package=Path("foo"), name=Path("bar.py")),
        BazelLabel(repo="ext", package=Path("pkg"), name=Path("target")),
    ]
    assert not any(isinstance(label, str) for label in result)


def test_run_query_raises_on_failure(tmp_path: Path) -> None:
    with (
        patch("bazel_util.query.subprocess.run", side_effect=CalledProcessError(1, "bazel")),
        pytest.raises(CalledProcessError),
    ):
        run_query("//...", cwd=tmp_path)


if __name__ == "__main__":
    pytest_bazel.main()
