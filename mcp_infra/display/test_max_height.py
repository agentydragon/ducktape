"""Tests for MaxHeight wrapper in rich_display."""

from __future__ import annotations

from io import StringIO

import pytest_bazel
from rich.console import Console
from rich.text import Text

from mcp_infra.display.rich_display import MaxHeight


def test_max_height_with_wrapping():
    """Test MaxHeight counts visual lines (after wrapping), not logical lines.

    Setup:
    - Console width = 30 characters
    - 20 logical lines, each 1000 characters long
    - max_height = 20 visual lines

    Expected behavior:
    - First logical line wraps into ~33 visual lines (1000 ÷ 30)
    - MaxHeight should show only first 20 visual lines
    - This means only ~600 chars of the first logical line
    - Then show truncation marker
    """
    # Create 20 logical lines, each 1000 chars long
    # Using 'A' for first line, 'B' for second, etc. to distinguish them
    logical_lines = []
    for i in range(20):
        char = chr(ord("A") + i)  # A, B, C, ...
        logical_lines.append(char * 1000)

    content_text = "\n".join(logical_lines)
    renderable = Text(content_text)

    # Wrap with MaxHeight(max_height=20)
    constrained = MaxHeight(renderable, max_height=20)

    # Create console with width=30, capture output
    output = StringIO()
    console = Console(file=output, width=30, legacy_windows=False)
    console.print(constrained)

    result = output.getvalue()
    lines = result.splitlines()

    # Verify results
    print(f"\nTotal visual lines: {len(lines)}")
    print(f"First line: {lines[0]!r}")
    print(f"Last line before truncation: {lines[-2]!r}")
    print(f"Truncation marker: {lines[-1]!r}")

    # Should have exactly 21 lines: 20 visual lines + 1 truncation marker
    assert len(lines) == 21, f"Expected 21 lines (20 content + 1 marker), got {len(lines)}"

    # First 20 lines should be all 'A' (first logical line, wrapped)
    for i in range(20):
        assert lines[i] == "A" * 30, f"Line {i} should be 30 A's, got: {lines[i]!r}"

    # Last line should be truncation marker
    # The first logical line wraps into ~33 visual lines, so we're hiding ~13 of them
    # Plus we have 19 more complete logical lines
    assert "more lines" in lines[-1], f"Expected truncation marker, got: {lines[-1]!r}"

    # Verify we ONLY see 'A' characters (first logical line)
    # Should NOT see 'B', 'C', etc. (subsequent logical lines)
    content_chars = "".join(lines[:-1])  # Exclude truncation marker
    assert all(c == "A" for c in content_chars), "Should only show first logical line (all A's)"
    assert "B" not in result, "Should not show second logical line"


def test_max_height_short_content():
    """Test MaxHeight doesn't pad short content."""
    # Create content shorter than max_height
    short_text = Text("Line 1\nLine 2\nLine 3")
    constrained = MaxHeight(short_text, max_height=20)

    output = StringIO()
    console = Console(file=output, width=80, legacy_windows=False)
    console.print(constrained)

    result = output.getvalue()
    lines = result.splitlines()

    # Should have exactly 3 lines (no padding, no truncation marker)
    assert len(lines) == 3, f"Expected 3 lines, got {len(lines)}"
    assert lines[0] == "Line 1"
    assert lines[1] == "Line 2"
    assert lines[2] == "Line 3"


def test_max_height_exact_limit():
    """Test MaxHeight when content exactly matches max_height."""
    # Create content with exactly max_height lines
    lines_content = "\n".join(f"Line {i}" for i in range(20))
    text = Text(lines_content)
    constrained = MaxHeight(text, max_height=20)

    output = StringIO()
    console = Console(file=output, width=80, legacy_windows=False)
    console.print(constrained)

    result = output.getvalue()
    lines = result.splitlines()

    # Should have exactly 20 lines (no truncation marker when at limit)
    assert len(lines) == 20, f"Expected 20 lines, got {len(lines)}"
    assert "more lines" not in result, "Should not show truncation marker at exact limit"


if __name__ == "__main__":
    pytest_bazel.main()
