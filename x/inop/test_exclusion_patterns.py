"""Tests for gitignore-style exclusion patterns."""

import pathspec
import pytest_bazel


def test_exclusion_patterns():
    """Test key gitignore pattern behaviors."""
    patterns = [
        "*.log",  # Wildcards
        "/CLAUDE.md",  # Root-only
        "temp/",  # Directories
        "build/**",  # Recursive
        "!debug.log",  # Negation
    ]
    spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)

    # Test cases
    assert spec.match_file("app.log")  # Wildcard match
    assert not spec.match_file("debug.log")  # Negated
    assert spec.match_file("CLAUDE.md")  # Root match
    assert not spec.match_file("sub/CLAUDE.md")  # Not root
    assert spec.match_file("temp/file.txt")  # Directory match
    assert spec.match_file("build/dist/app.js")  # Recursive match


if __name__ == "__main__":
    pytest_bazel.main()
