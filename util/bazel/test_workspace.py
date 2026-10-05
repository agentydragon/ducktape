"""Tests for util.bazel.workspace."""

from __future__ import annotations

from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess
from unittest.mock import patch

import pytest
import pytest_bazel

from util.bazel.workspace import BazelBackend, BazelLabel, BazelWorkspace


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("//foo/bar:baz.py", BazelLabel(repo="", package=Path("foo/bar"), name="baz.py")),
        ("//:root.txt", BazelLabel(repo="", package=Path(), name="root.txt")),
        ("//a/b/c:d/e.rs", BazelLabel(repo="", package=Path("a/b/c"), name="d/e.rs")),
        ("@repo//pkg:target", BazelLabel(repo="repo", package=Path("pkg"), name="target")),
        ("@@canonical//pkg:target", BazelLabel(repo="canonical", package=Path("pkg"), name="target")),
        # Short form: //pkg implies //pkg:pkg
        ("//foo/bar", BazelLabel(repo="", package=Path("foo/bar"), name="bar")),
        (
            "//cluster/scripts/validate_cluster/cmd/main",
            BazelLabel(repo="", package=Path("cluster/scripts/validate_cluster/cmd/main"), name="main"),
        ),
        ("@repo//pkg", BazelLabel(repo="repo", package=Path("pkg"), name="pkg")),
    ],
)
def test_parse_valid(raw: str, expected: BazelLabel) -> None:
    assert BazelLabel.parse(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "foo:bar",  # no // prefix
        "",  # empty
        "@repo",  # @ but no //
        "@@canonical",  # @@ but no //
    ],
)
def test_parse_invalid_raises(raw: str) -> None:
    with pytest.raises(ValueError, match=repr(raw) if raw else ""):
        BazelLabel.parse(raw)


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
        (BazelLabel(repo="", package=Path("foo/bar"), name="baz.py"), "//foo/bar:baz.py"),
        (BazelLabel(repo="", package=Path(), name="root.txt"), "//:root.txt"),
        (BazelLabel(repo="repo", package=Path("pkg"), name="target"), "@repo//pkg:target"),
        (BazelLabel(repo="canonical", package=Path("pkg"), name="target"), "@canonical//pkg:target"),
        # Short form: name matches last package component
        (BazelLabel(repo="", package=Path("foo/bar"), name="bar"), "//foo/bar"),
        (
            BazelLabel(repo="", package=Path("cluster/scripts/validate_cluster/cmd/main"), name="main"),
            "//cluster/scripts/validate_cluster/cmd/main",
        ),
        (BazelLabel(repo="repo", package=Path("pkg"), name="pkg"), "@repo//pkg"),
    ],
)
def test_str(label: BazelLabel, expected: str) -> None:
    assert str(label) == expected


def test_query_parses_labels(tmp_path: Path) -> None:
    completed = CompletedProcess(args=[], returncode=0, stdout="//foo:bar.py\n@ext//pkg:target\n\n", stderr="")
    workspace = BazelWorkspace(root=tmp_path, backend=BazelBackend.LOCAL)
    with patch("util.bazel.workspace.subprocess.run", return_value=completed) as mock_run:
        result = workspace.query("//...")
    (cmd,), kwargs = mock_run.call_args
    assert cmd[:3] == ["bazelisk", "query", "--output=label"]
    assert cmd[3] == "//..."  # short queries passed inline, not via --query_file
    assert kwargs == {"capture_output": True, "text": True, "cwd": tmp_path, "check": False, "timeout": None}
    assert result == [
        BazelLabel(repo="", package=Path("foo"), name="bar.py"),
        BazelLabel(repo="ext", package=Path("pkg"), name="target"),
    ]
    assert not any(isinstance(label, str) for label in result)


def test_query_persist_dir(tmp_path: Path) -> None:
    persist_dir = tmp_path / "persist"
    persist_dir.mkdir()
    completed = CompletedProcess(args=[], returncode=0, stdout="//foo:bar\n", stderr="")
    workspace = BazelWorkspace(root=tmp_path, backend=BazelBackend.LOCAL)
    with patch("util.bazel.workspace.subprocess.run", return_value=completed):
        workspace.query("//...", persist_dir=persist_dir)
    assert (persist_dir / "query").read_text() == "//..."
    assert (persist_dir / "stdout").read_text() == "//foo:bar\n"
    assert (persist_dir / "exit_code").read_text() == "0"


def test_query_filters_bbr_log_lines(tmp_path: Path) -> None:
    """bbr mixes its own log lines into stdout; query() must filter them."""
    stdout = (
        "Streaming remote runner logs to: https://app.buildbuddy.io/invocation/0b50b97b\n"
        "\x1b[90m2026-04-08 14:38:25.577 UTC \x1b[mSyncing existing repo...\n"
        "\x1b[90m2026-04-08 14:38:25.577 UTC \x1b[mConfiguring repository...\n"
        '\x1b[90m2026-04-08 14:38:25.641 UTC \x1b[mConfiguring remote "origin"...\n'
        "\x1b[90m2026-04-08 14:38:25.643 UTC \x1b[32m$ \x1b[mgit fetch --force --depth=1 origin abc123\n"
        "remote: Total 0 (delta 0), reused 0 (delta 0), pack-reused 0 (from 0)\n"
        "From https://github.com/agentydragon/ducktape\n"
        " * branch            abc123 -> FETCH_HEAD\n"
        "\x1b[32mLoading: \x1b[m12 packages loaded\n"
        "//\n"
        "//devinfra/precommit:test_filename_conventions.py\n"
        "//util/bazel:test_workspace.py\n"
        "@pypi//pytest:pkg\n"
        "\x1b[32mINFO: \x1b[mStreaming build results to: https://app.buildbuddy.io/invocation/0b50b97b\n"
        "\x1b[90m2026-04-08 14:38:30.000 UTC (command exited with code 0)\n"
    )
    completed = CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")
    workspace = BazelWorkspace(root=tmp_path, backend=BazelBackend.LOCAL)
    with patch("util.bazel.workspace.subprocess.run", return_value=completed):
        result = workspace.query("//...")
    assert result == [
        BazelLabel(repo="", package=Path("devinfra/precommit"), name="test_filename_conventions.py"),
        BazelLabel(repo="", package=Path("util/bazel"), name="test_workspace.py"),
        BazelLabel(repo="pypi", package=Path("pytest"), name="pkg"),
    ]


def test_query_raises_on_failure_with_stderr_in_the_traceback(tmp_path: Path) -> None:
    completed = CompletedProcess(args=[], returncode=7, stdout="", stderr="ERROR: no such package 'missing'")
    workspace = BazelWorkspace(root=tmp_path, backend=BazelBackend.LOCAL)
    with (
        patch("util.bazel.workspace.subprocess.run", return_value=completed),
        pytest.raises(CalledProcessError) as excinfo,
    ):
        workspace.query("//...")
    assert excinfo.value.__notes__ == ["ERROR: no such package 'missing'"]


if __name__ == "__main__":
    pytest_bazel.main()
