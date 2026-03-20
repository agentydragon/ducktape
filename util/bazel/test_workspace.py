"""Tests for util.bazel.workspace."""

from __future__ import annotations

from pathlib import Path
from subprocess import CalledProcessError
from unittest.mock import MagicMock, patch

import pytest
import pytest_bazel

from util.bazel.workspace import BazelInfoResult, BazelLabel, BazelWorkspace, parse_info_output


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
        ("//cluster/kubespand/cmd/apid", BazelLabel(repo="", package=Path("cluster/kubespand/cmd/apid"), name="apid")),
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


@pytest.mark.parametrize("raw", ["foo:bar", ""])
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
        (BazelLabel(repo="", package=Path("devinfra/orphans"), name="find_orphans"), Path("devinfra/orphans")),
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
        # Short form: name matches last package component
        (BazelLabel(repo="", package=Path("foo/bar"), name="bar"), "//foo/bar"),
        (BazelLabel(repo="", package=Path("cluster/kubespand/cmd/apid"), name="apid"), "//cluster/kubespand/cmd/apid"),
        (BazelLabel(repo="repo", package=Path("pkg"), name="pkg"), "@repo//pkg"),
    ],
)
def test_str(label: BazelLabel, expected: str) -> None:
    assert str(label) == expected


def test_query_parses_labels(tmp_path: Path) -> None:
    mock_result = MagicMock()
    mock_result.stdout = "//foo:bar.py\n@ext//pkg:target\n\n"
    mock_result.returncode = 0
    workspace = BazelWorkspace(root=tmp_path)
    with patch("util.bazel.workspace.subprocess.run", return_value=mock_result) as mock_run:
        result = workspace.query("//...")
    (cmd,), kwargs = mock_run.call_args
    assert cmd[:3] == ["bazel", "query", "--output=label"]
    assert cmd[3].startswith("--query_file=")
    assert kwargs == {"capture_output": True, "text": True, "cwd": tmp_path, "check": False, "timeout": None}
    assert result == [
        BazelLabel(repo="", package=Path("foo"), name="bar.py"),
        BazelLabel(repo="ext", package=Path("pkg"), name="target"),
    ]
    assert not any(isinstance(label, str) for label in result)


def test_query_persist_dir(tmp_path: Path) -> None:
    persist_dir = tmp_path / "persist"
    persist_dir.mkdir()
    mock_result = MagicMock()
    mock_result.stdout = "//foo:bar\n"
    mock_result.stderr = ""
    mock_result.returncode = 0
    workspace = BazelWorkspace(root=tmp_path)
    with patch("util.bazel.workspace.subprocess.run", return_value=mock_result):
        workspace.query("//...", persist_dir=persist_dir)
    assert (persist_dir / "query").read_text() == "//..."
    assert (persist_dir / "stdout").read_text() == "//foo:bar\n"
    assert (persist_dir / "exit_code").read_text() == "0"


def test_query_raises_on_failure(tmp_path: Path) -> None:
    mock_result = MagicMock()
    mock_result.stdout = ""
    mock_result.stderr = "error"
    mock_result.returncode = 1
    workspace = BazelWorkspace(root=tmp_path)
    with patch("util.bazel.workspace.subprocess.run", return_value=mock_result), pytest.raises(CalledProcessError):
        workspace.query("//...")


# --- parse_info_output / BazelInfoResult tests ---


def test_parse_info_output_full():
    output = "server_pid: 12345\noutput_base: /home/user/.cache/bazel/abc123\nrelease: release 8.5.0\n"
    result = parse_info_output(output)
    assert result.server_pid == 12345
    assert result.output_base == Path("/home/user/.cache/bazel/abc123")
    assert result.release == "release 8.5.0"


def test_parse_info_output_empty():
    result = parse_info_output("")
    assert result == BazelInfoResult()


def test_parse_info_output_partial():
    result = parse_info_output("server_pid: 99\n")
    assert result.server_pid == 99
    assert result.output_base is None


def test_parse_info_output_hyphenated_keys():
    output = "bazel-bin: /tmp/bin\njava-home: /usr/lib/jvm\ngc-count: 42\n"
    result = parse_info_output(output)
    assert result.bazel_bin == Path("/tmp/bin")
    assert result.java_home == Path("/usr/lib/jvm")
    assert result.gc_count == 42


def test_parse_info_output_unknown_keys_ignored():
    output = "server_pid: 1\nsome_future_key: whatever\n"
    result = parse_info_output(output)
    assert result.server_pid == 1


def test_binary_field():
    ws = BazelWorkspace(root=Path("/tmp"), binary="/custom/bazel")
    assert ws._bazel_prefix()[0] == "/custom/bazel"


def test_binary_field_default():
    ws = BazelWorkspace(root=Path("/tmp"))
    assert ws._bazel_prefix()[0] == "bazel"


if __name__ == "__main__":
    pytest_bazel.main()
