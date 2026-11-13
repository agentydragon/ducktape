"""Tests for find_orphans whitelist utilities."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest_bazel

from devinfra.bazel.workspace import BazelBackend, BazelLabel, BazelWorkspace
from devinfra.orphans.find_orphans import PatternStats, main, run_report


def _run(
    tmp_path: Path, whitelist_text: str, git_files: set[Path], bazel_files: set[Path]
) -> tuple[list[Path], list[PatternStats]]:
    whitelist = tmp_path / "whitelist.txt"
    whitelist.write_text(whitelist_text)
    with (
        patch("devinfra.orphans.find_orphans.get_git_files", return_value=git_files),
        patch("devinfra.orphans.find_orphans.query_bazel_files", return_value=bazel_files),
    ):
        return run_report(BazelWorkspace(root=tmp_path, backend=BazelBackend.LOCAL), whitelist)


def _unused(stats: list[PatternStats]) -> list[str]:
    return [s.pattern for s in stats if s.orphaned == 0]


def test_all_patterns_used_returns_empty(tmp_path: Path) -> None:
    orphans, stats = _run(
        tmp_path, "foo/**\nbaz/**\n", git_files={Path("foo/bar.txt"), Path("baz/qux.py")}, bazel_files=set()
    )
    assert orphans == []
    assert _unused(stats) == []


def test_unmatched_pattern_reported(tmp_path: Path) -> None:
    _, stats = _run(tmp_path, "foo/**\nnonexistent/**\n", git_files={Path("foo/bar.txt")}, bazel_files=set())
    assert _unused(stats) == ["nonexistent/**"]


def test_comments_and_blanks_ignored(tmp_path: Path) -> None:
    _, stats = _run(tmp_path, "# This is a comment\n\n   \n# Another comment\n", git_files=set(), bazel_files=set())
    assert _unused(stats) == []


def test_extension_pattern_matches_orphan(tmp_path: Path) -> None:
    _, stats = _run(
        tmp_path,
        "*.md\n*.json\n*.txt\n",
        git_files={Path("docs/readme.md"), Path("config/settings.json")},
        bazel_files=set(),
    )
    assert _unused(stats) == ["*.txt"]


def test_bazelized_files_not_orphans(tmp_path: Path) -> None:
    orphans, _ = _run(
        tmp_path, "", git_files={Path("foo/bar.py"), Path("foo/baz.py")}, bazel_files={Path("foo/bar.py")}
    )
    assert orphans == [Path("foo/baz.py")]


def test_pattern_stats_bazel_covered(tmp_path: Path) -> None:
    # Pattern matches both a Bazel-covered file and an orphan.
    _, stats = _run(tmp_path, "*.py\n", git_files={Path("a.py"), Path("b.py")}, bazel_files={Path("a.py")})
    assert len(stats) == 1
    assert stats[0].pattern == "*.py"
    assert stats[0].bazel_covered == 1
    assert stats[0].orphaned == 1


def test_dead_pattern_with_bazel_covered(tmp_path: Path) -> None:
    # All matching files are in Bazel → pattern is dead (orphaned == 0).
    _, stats = _run(tmp_path, "*.py\n", git_files={Path("a.py")}, bazel_files={Path("a.py")})
    assert _unused(stats) == ["*.py"]
    assert stats[0].bazel_covered == 1
    assert stats[0].orphaned == 0


def test_keep_going_applies_to_source_file_query(tmp_path: Path, monkeypatch, capsys) -> None:
    whitelist = tmp_path / "whitelist.txt"
    whitelist.write_text("")

    keep_going_values: list[bool] = []

    class PartialQueryWorkspace:
        root = tmp_path

        def query(self, _expr: str, *, keep_going: bool = False) -> list[BazelLabel]:
            keep_going_values.append(keep_going)
            if not keep_going:
                raise subprocess.CalledProcessError(3, "bazel query")
            return [BazelLabel.parse("//pkg:kept.py")]

    workspace = PartialQueryWorkspace()
    monkeypatch.setattr("devinfra.orphans.find_orphans.get_build_workspace_directory", lambda: tmp_path)
    monkeypatch.setattr("devinfra.orphans.find_orphans.detect_bazel_backend", lambda: BazelBackend.LOCAL)
    monkeypatch.setattr("devinfra.orphans.find_orphans.BazelWorkspace", lambda **_kwargs: workspace)
    monkeypatch.setattr(
        "devinfra.orphans.find_orphans.get_git_files", lambda _root: {Path("pkg/kept.py"), Path("pkg/unreturned.py")}
    )
    monkeypatch.setattr("devinfra.orphans.find_orphans.query_orphan_py_libraries", lambda *_args, **_kwargs: set())

    monkeypatch.setattr("sys.argv", ["find_orphans", "--whitelist", str(whitelist)])
    try:
        main()
    except subprocess.CalledProcessError:
        pass
    else:
        raise AssertionError("the source-file query should fail without --keep-going")

    monkeypatch.setattr("sys.argv", ["find_orphans", "--keep-going", "--whitelist", str(whitelist)])
    main()

    assert keep_going_values == [False, True]
    assert capsys.readouterr().out == "pkg/unreturned.py\n"


if __name__ == "__main__":
    pytest_bazel.main()
