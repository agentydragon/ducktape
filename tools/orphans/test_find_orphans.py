"""Tests for find_orphans whitelist utilities."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest_bazel

from tools.orphans.find_orphans import PatternStats, run_report


def _run(
    tmp_path: Path, whitelist_text: str, git_files: set[Path], bazel_files: set[Path]
) -> tuple[list[Path], list[PatternStats]]:
    whitelist = tmp_path / "whitelist.txt"
    whitelist.write_text(whitelist_text)
    with (
        patch("tools.orphans.find_orphans.get_git_files", return_value=git_files),
        patch("tools.orphans.find_orphans.query_bazel_files", return_value=bazel_files),
    ):
        return run_report(tmp_path, whitelist)


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


def test_all_patterns_unused_with_empty_orphans(tmp_path: Path) -> None:
    _, stats = _run(tmp_path, "*.txt\nfoo/**\nbar.py\n", git_files=set(), bazel_files=set())
    assert _unused(stats) == ["*.txt", "foo/**", "bar.py"]


def test_glob_pattern_matches_orphan(tmp_path: Path) -> None:
    orphans, stats = _run(tmp_path, "project/**\n", git_files={Path("project/src/main.py")}, bazel_files=set())
    assert orphans == []
    assert _unused(stats) == []


def test_extension_pattern_matches_orphan(tmp_path: Path) -> None:
    _, stats = _run(
        tmp_path,
        "*.md\n*.json\n*.txt\n",
        git_files={Path("docs/readme.md"), Path("config/settings.json")},
        bazel_files=set(),
    )
    assert _unused(stats) == ["*.txt"]


def test_exact_path_pattern_matches(tmp_path: Path) -> None:
    _, stats = _run(
        tmp_path,
        "tools/special-script\ntools/other-script\n",
        git_files={Path("tools/special-script")},
        bazel_files=set(),
    )
    assert _unused(stats) == ["tools/other-script"]


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


if __name__ == "__main__":
    pytest_bazel.main()
