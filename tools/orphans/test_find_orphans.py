"""Tests for find_orphans whitelist utilities."""

from __future__ import annotations

from pathlib import Path

import pytest_bazel

from tools.orphans.find_orphans import unused_whitelist_patterns


def test_all_patterns_used_returns_empty() -> None:
    raw_orphans = {Path("foo/bar.txt"), Path("baz/qux.py")}
    lines = ["foo/**", "baz/**"]
    assert unused_whitelist_patterns(raw_orphans, lines) == []


def test_unmatched_pattern_reported() -> None:
    raw_orphans = {Path("foo/bar.txt")}
    lines = ["foo/**", "nonexistent/**"]
    assert unused_whitelist_patterns(raw_orphans, lines) == ["nonexistent/**"]


def test_comments_and_blanks_ignored() -> None:
    raw_orphans: set[Path] = set()
    lines = ["# This is a comment", "", "   ", "# Another comment"]
    assert unused_whitelist_patterns(raw_orphans, lines) == []


def test_all_patterns_unused_with_empty_orphans() -> None:
    raw_orphans: set[Path] = set()
    lines = ["*.txt", "foo/**", "bar.py"]
    assert unused_whitelist_patterns(raw_orphans, lines) == ["*.txt", "foo/**", "bar.py"]


def test_glob_pattern_matches_orphan() -> None:
    raw_orphans = {Path("project/src/main.py")}
    lines = ["project/**"]
    assert unused_whitelist_patterns(raw_orphans, lines) == []


def test_extension_pattern_matches_orphan() -> None:
    raw_orphans = {Path("docs/readme.md"), Path("config/settings.json")}
    lines = ["*.md", "*.json", "*.txt"]
    # *.txt matches nothing
    assert unused_whitelist_patterns(raw_orphans, lines) == ["*.txt"]


def test_exact_path_pattern_matches() -> None:
    raw_orphans = {Path("tools/special-script")}
    lines = ["tools/special-script", "tools/other-script"]
    assert unused_whitelist_patterns(raw_orphans, lines) == ["tools/other-script"]


if __name__ == "__main__":
    pytest_bazel.main()
