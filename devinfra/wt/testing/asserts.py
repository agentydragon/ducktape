"""Shared assertions and parsing helpers for WT CLI output tests."""

from __future__ import annotations

import re

from hamcrest import assert_that, contains_string


def extract_status_rows(output: str) -> dict[str, str]:
    """Parse status output into a mapping of worktree name -> full line.

    Filters out spinner/header lines and reduces access to rows by name.
    """
    lines = [ln for ln in output.splitlines() if ln and not ln.startswith(("✓", "⟳"))]
    rows: dict[str, str] = {}
    for ln in lines:
        name = ln.split(maxsplit=1)[0]
        rows[name] = ln
    return rows


def status_row_ok(line: str, *, must_contain: list[str] | None = None, commit_re: str = r"[0-9a-f]{8}\b") -> bool:
    """Validate a status row has an 8-hex commit and required substrings."""
    must = must_contain or ["clean", " running"]
    # name, spaces, 8-hex commit, spaces, rest
    if not re.match(rf"^[^\s]+\s+{commit_re}", line):
        return False
    return all(part in line for part in must)


def assert_output_contains(output: str, *snippets: str) -> None:
    """Assert that CLI output contains each provided substring."""

    for snippet in snippets:
        assert_that(output, contains_string(str(snippet)))
