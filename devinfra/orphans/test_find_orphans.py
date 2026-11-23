"""Tests for find_orphans whitelist utilities."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devinfra.bazel.workspace import BazelBackend
from devinfra.orphans.find_orphans import PatternStats, classify_files, main


def test_overlapping_patterns_count_all_matches() -> None:
    orphans, stats = classify_files(
        {Path("foo/tracked.py"), Path("foo/orphan.py"), Path("bar.txt"), Path("built.txt")},
        {Path("foo/tracked.py"), Path("external/input.py"), Path("built.txt")},
        ["# comment", " ", " foo/** ", "*.py", "missing/**"],
    )
    assert orphans == [Path("bar.txt")]
    assert stats == [
        PatternStats("foo/**", bazel_covered=1, orphaned=1),
        PatternStats("*.py", bazel_covered=1, orphaned=1),
        PatternStats("missing/**", bazel_covered=0, orphaned=0),
    ]


def test_pattern_matching_only_bazel_inputs_is_dead() -> None:
    orphans, stats = classify_files({Path("a.py"), Path("b.txt")}, {Path("a.py")}, ["*.py"])
    assert orphans == [Path("b.txt")]
    assert stats == [PatternStats("*.py", bazel_covered=1, orphaned=0)]


def test_keep_going_applies_to_source_file_query(tmp_path: Path, monkeypatch, capsys) -> None:
    whitelist = tmp_path / "whitelist.txt"
    whitelist.write_text("")

    flags = []

    def partial_query(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        flags.append("--keep_going" in command)
        stdout = "//pkg:kept.py\n" if "source file" in command[-1] else ""
        return subprocess.CompletedProcess(command, 3, stdout, "one package failed")

    monkeypatch.setenv("BUILD_WORKSPACE_DIRECTORY", str(tmp_path))
    monkeypatch.setattr("devinfra.orphans.find_orphans.detect_bazel_backend", lambda: BazelBackend.LOCAL)
    monkeypatch.setattr("devinfra.bazel.workspace.subprocess.run", partial_query)
    monkeypatch.setattr(
        "devinfra.orphans.find_orphans.get_git_files", lambda _root: {Path("pkg/kept.py"), Path("pkg/unreturned.py")}
    )

    monkeypatch.setattr("sys.argv", ["find_orphans", "--whitelist", str(whitelist)])
    with pytest.raises(subprocess.CalledProcessError):
        main()

    monkeypatch.setattr("sys.argv", ["find_orphans", "--keep-going", "--whitelist", str(whitelist)])
    main()

    assert flags == [False, True, True]
    assert capsys.readouterr().out == "pkg/unreturned.py\n"
