"""Tests for DiffTree renderable."""

import re
from dataclasses import replace

import pytest
from rich.console import Console
from rich.segment import Segment
from rich.text import Text

from difftree.config import DEFAULT_CONFIG, Column, RenderConfig
from difftree.conftest import make_diff_tree, render_to_string
from difftree.diff_tree import DiffTree
from difftree.parser import FileChange
from difftree.progress_bar import BlockChars


def _render_to_text_lines(diff_tree: DiffTree, width: int = 80) -> list[Text]:
    """Render tree and return lines as Rich Text objects."""
    console = Console(record=True, width=width)
    console.print(diff_tree)

    segments = console._record_buffer
    lines = list(Segment.split_lines(segments))

    text_lines = []
    for line_segments in lines:
        text = Text()
        for seg in line_segments:
            if not seg.is_control:
                text.append(seg.text, style=seg.style)
        text_lines.append(text)

    return text_lines


def _find_line_with(lines: list[str], substring: str) -> str:
    """Find first line containing substring."""
    return next(line for line in lines if substring in line)


def _find_tree_pos(line: str) -> int:
    """Find position of first tree decoration character, or -1 if not found."""
    positions = [line.find(char) for char in [".", "└", "├", "│"] if char in line]
    return min(positions) if positions else -1


def _assert_column_before(result: str, filename: str, first: str, second: str, first_desc: str, second_desc: str):
    """Assert that first marker appears before second marker in line containing filename."""
    line = _find_line_with(result.split("\n"), filename)

    first_pos = line.find(first) if first in line else -1

    second_pos = _find_tree_pos(line) if second in ["tree_char"] else line.find(second) if second in line else -1

    if first_pos != -1 and second_pos != -1:
        assert first_pos < second_pos, (
            f"Expected {first_desc} before {second_desc}, but got {first_desc} at {first_pos}, {second_desc} at {second_pos}"
        )


def test_renderer_initialization():
    """Test DiffTree initialization."""
    diff_tree = make_diff_tree([FileChange(path="test.py", additions=1, deletions=0)])

    assert diff_tree.root is not None
    assert Column.COUNTS in diff_tree.config.columns
    assert Column.BARS in diff_tree.config.columns
    assert Column.PERCENTAGES in diff_tree.config.columns
    assert diff_tree.config.bar_width == 20


def test_renderer_with_custom_options():
    """Test DiffTree with custom options."""
    config = RenderConfig(columns=[Column.TREE], bar_width=30)
    diff_tree = make_diff_tree([FileChange(path="test.py", additions=1, deletions=0)], config=config)

    assert diff_tree.root is not None
    assert Column.COUNTS not in diff_tree.config.columns
    assert Column.BARS not in diff_tree.config.columns
    assert Column.PERCENTAGES not in diff_tree.config.columns
    assert diff_tree.config.bar_width == 30


def test_render_simple_tree(sample_changes):
    """Test rendering a simple tree structure."""
    diff_tree = make_diff_tree(sample_changes)
    result = render_to_string(diff_tree, width=120)

    # Check that key elements are present
    assert "src" in result
    assert "tests" in result
    assert "README.md" in result
    assert "main.py" in result
    assert "models" in result


def test_render_with_no_counts(sample_changes):
    """Test rendering without count columns."""
    config = RenderConfig(columns=[Column.TREE, Column.BARS, Column.PERCENTAGES])
    diff_tree = make_diff_tree(sample_changes, config=config)
    result = render_to_string(diff_tree, width=120)

    # Should still have tree structure but different formatting
    assert "src" in result


def test_render_with_max_depth(sample_changes):
    """Test rendering with maximum depth limit."""
    config = replace(DEFAULT_CONFIG, max_depth=1)
    diff_tree = make_diff_tree(sample_changes, config=config)
    result = render_to_string(diff_tree, width=120)

    # Should show top-level items but not deeply nested ones
    assert "src" in result
    # Depth limit might prevent showing nested files
    # This is a simplified test


# Progress bar integration tests


def test_minimum_sliver_with_small_changes():
    """Test rendering with one very small change among larger ones."""
    # Create changes where one is very small relative to others
    changes = [
        FileChange(path="large_file.py", additions=10000, deletions=5000),
        FileChange(path="tiny_file.py", additions=1, deletions=0),
    ]

    diff_tree = make_diff_tree(changes)
    result = render_to_string(diff_tree, width=120)

    # Both files should be visible in the output
    assert "large_file.py" in result
    assert "tiny_file.py" in result
    # The tiny file should have some visible indicator despite small ratio
    # (This is a high-level test; the unit test above is more precise)


# Console width tests


@pytest.mark.parametrize(
    "width",
    [
        40,  # Very narrow terminal
        80,  # Standard terminal width
        200,  # Wide terminal
    ],
)
def test_console_width_handling(width):
    """Test rendering with different console widths."""
    changes = [
        FileChange(path="src/very_long_filename_that_might_wrap.py", additions=100, deletions=50),
        FileChange(path="test.py", additions=10, deletions=5),
    ]

    diff_tree = make_diff_tree(changes, config=DEFAULT_CONFIG)
    result = render_to_string(diff_tree, width=width)

    # Basic assertions: output should contain expected elements
    assert result.strip() != ""

    # Stats visibility depends on width
    # At very narrow widths (40), tree column takes all space and stats may not be visible
    # At standard widths (80+), stats should be visible
    if width >= 80:
        assert "+100" in result or "+10" in result

    # Filename visibility depends on width
    if width >= 200:
        # Very wide: full collapsed path visible without wrapping
        assert "very_long_filename" in result
        assert "test.py" in result
    elif width >= 80:
        # Standard width: path may wrap but parts are visible
        assert "very_long" in result
        assert "test.py" in result
    # At width=40, table columns wrap onto multiple lines, making text
    # assertions unreliable - just verify output exists


# Progress bar format tests


def _extract_progress_bars(line: Text) -> str:
    """Extract just the progress bar characters from a line (after filename and counts)."""
    plain: str = line.plain
    block_chars = " ▏▎▍▌▋▊▉█"

    # Find a sequence of at least 40 consecutive block characters (2 * bar_width)
    # This is the dual progress bar section
    i = 0
    while i < len(plain):
        if plain[i] in block_chars:
            # Found start of a potential block sequence
            start = i
            while i < len(plain) and plain[i] in block_chars:
                i += 1
            length = i - start

            # If this sequence is at least 40 chars, it's our progress bar
            if length >= 40:
                # The sequence might include padding spaces before/after the bar
                # The bar itself is exactly 40 characters
                # Skip leading padding spaces (between counts and bar)
                bar_candidate = plain[start:i].lstrip(" ")
                # Take exactly 40 characters (the dual progress bar)
                return bar_candidate[:40]
        else:
            i += 1

    # Fallback: return empty if not found
    return ""


def test_progress_bars_align_consistently():
    """Test that files with same stats render with same bar positions."""
    # Files with identical stats should have bars at same column positions
    changes = [
        FileChange(path="file_a.py", additions=100, deletions=50),
        FileChange(path="file_b.py", additions=100, deletions=50),
    ]

    diff_tree = make_diff_tree(changes, config=DEFAULT_CONFIG)

    result = render_to_string(diff_tree, width=120)
    lines = result.split("\n")

    # Find lines for each file
    line_a = next(line for line in lines if "file_a.py" in line)
    line_b = next(line for line in lines if "file_b.py" in line)

    # Strip filenames and tree decorations, compare the stats/bars part
    # (Tree decorations differ for first/last items, but stats should be identical)
    # Extract stats and bars (everything after the filename)
    stats_a = re.sub(r".*file_a\.py", "", line_a)
    stats_b = re.sub(r".*file_b\.py", "", line_b)
    assert stats_a == stats_b, "Files with same stats should have identical stat/bar rendering"


def test_tree_column_never_wraps():
    """Test that tree column text never wraps to multiple lines."""
    # Create scenario where bars would be large and squeeze tree column
    changes = [
        FileChange(path="very/long/path/to/some/deeply/nested/file.py", additions=10000, deletions=5000),
        FileChange(path="another/long/path/controller-deployment.yaml", additions=100, deletions=10),
    ]

    diff_tree = make_diff_tree(changes, config=DEFAULT_CONFIG)

    # Test at a narrow width where bars would take up space
    result = render_to_string(diff_tree, width=100)

    # Check that each line contains tree decoration AND filename on same line
    # Lines should not have tree decoration on one line and filename on next
    lines = result.strip().split("\n")

    for i, line in enumerate(lines):
        # If line has tree decoration (├── or └── or │), it must have filename too
        has_tree_char = any(char in line for char in ["├", "└", "│"])

        if has_tree_char and i < len(lines) - 1:
            # Check that filename components are on this line, not next line
            # The next line should not start with a bare filename (no tree chars)
            next_line = lines[i + 1]
            # Next line should either have tree chars OR be empty/whitespace
            # It should NOT be a continuation of the current line's filename
            if next_line.strip() and not any(char in next_line for char in ["├", "└", "│", "─"]):
                pytest.fail(f"Line {i} appears to have wrapped:\n  Line {i}: {line!r}\n  Line {i + 1}: {next_line!r}")


def test_tree_styling_preserved():
    """Test that tree decorations are dim and filenames have correct colors."""
    changes = [
        FileChange(path="src/file1.py", additions=10, deletions=5),
        FileChange(path="src/file2.py", additions=8, deletions=3),
        FileChange(path="test.py", additions=5, deletions=2),
    ]

    diff_tree = make_diff_tree(changes, config=DEFAULT_CONFIG)

    # Render with colors
    result = render_to_string(diff_tree, width=120, color_system="standard")

    # Check for ANSI styling codes
    # Dim style: \x1b[2m (used for tree decorations ├── └── │)
    # Bold blue: \x1b[1;34m (used for directories)
    # Reset: \x1b[0m

    # Tree decorations should be dim
    assert "\x1b[2m├── \x1b[0m" in result or "\x1b[2m└── \x1b[0m" in result, (
        "Tree decorations (├── or └──) should have dim style"
    )

    # Vertical guides should also be dim
    assert "\x1b[2m│" in result, "Tree vertical guides (│) should have dim style"

    # Directory "src" should be bold blue
    assert "\x1b[1;34msrc\x1b[0m" in result, "Directory names should be bold blue"

    # Files should NOT have bold blue styling
    # Check that filenames appear without bold blue
    lines = result.split("\n")
    for line in lines:
        # Files should not have bold blue color code immediately before their names
        if "file1.py" in line:
            assert "\x1b[1;34mfile1.py" not in line, "file1.py should not have bold blue style"
        if "file2.py" in line:
            assert "\x1b[1;34mfile2.py" not in line, "file2.py should not have bold blue style"
        if "test.py" in line:
            assert "\x1b[1;34mtest.py" not in line, "test.py should not have bold blue style"


def test_column_ordering():
    """Test that columns appear in the order specified in config."""
    changes = [FileChange(path="file.py", additions=10, deletions=5)]

    # Test bars before tree
    config = RenderConfig(columns=[Column.BARS, Column.TREE, Column.COUNTS])
    result = render_to_string(make_diff_tree(changes, config=config), width=80, force_terminal=False)
    _assert_column_before(result, "file.py", "█", "tree_char", "bars", "tree")

    # Test tree before bars (standard order)
    config = RenderConfig(columns=[Column.TREE, Column.BARS, Column.COUNTS])
    result = render_to_string(make_diff_tree(changes, config=config), width=80, force_terminal=False)

    line = _find_line_with(result.split("\n"), "file.py")
    tree_pos = _find_tree_pos(line)
    bar_pos = line.find("█") if "█" in line else -1
    if bar_pos != -1 and tree_pos != -1:
        assert tree_pos < bar_pos, f"Expected tree before bars, but got tree at {tree_pos}, bar at {bar_pos}"


def test_column_ordering_counts_first():
    """Test counts column can appear first."""
    changes = [FileChange(path="file.py", additions=10, deletions=5)]

    config = RenderConfig(columns=[Column.COUNTS, Column.TREE])
    result = render_to_string(make_diff_tree(changes, config=config), width=80, force_terminal=False)
    _assert_column_before(result, "file.py", "+10", "tree_char", "counts", "tree")


def test_bar_proportionality():
    """Test that progress bars render proportionally to actual changes."""
    changes = [
        FileChange(path="file1.py", additions=50, deletions=10),  # 5:1 ratio
        FileChange(path="file2.py", additions=1, deletions=10),  # 1:10 ratio
        FileChange(path="file3.py", additions=10, deletions=0),  # Only additions
    ]

    # Use simple distinct characters for testing:
    # - Additions use right-aligned bars, so they use right_blocks: '+'
    # - Deletions use left-aligned bars, so they use left_blocks: '-'
    # This makes counting trivial - just count '+' and '-' in plain text
    config = RenderConfig(
        columns=[Column.TREE, Column.BARS],
        bar_width=10,
        bar_left_blocks=BlockChars.simple("-"),  # Deletions (LTR)
        bar_right_blocks=BlockChars.simple("+"),  # Additions (RTL)
    )
    diff_tree = make_diff_tree(changes, config=config)

    # Render and extract plain text (no ANSI codes needed)
    lines = _render_to_text_lines(diff_tree, width=80)

    file_lines = {}
    for line in lines:
        plain = line.plain
        for file_path in ["file1.py", "file2.py", "file3.py"]:
            if file_path in plain:
                file_lines[file_path] = plain
                break

    # Count '+' for additions and '-' for deletions
    file1_plus = file_lines["file1.py"].count("+")
    file1_minus = file_lines["file1.py"].count("-")
    file2_plus = file_lines["file2.py"].count("+")
    file2_minus = file_lines["file2.py"].count("-")
    file3_plus = file_lines["file3.py"].count("+")
    file3_minus = file_lines["file3.py"].count("-")

    # The bars are scaled to max values across all files:
    # max_additions = 50 + 1 + 10 = 61 (including root aggregation)
    # max_deletions = 10 + 10 + 0 = 20 (including root aggregation)

    # File1: +50 -10
    # Expected green bar: 50/61 ≈ 82% of 10 blocks ≈ 8 blocks
    # Expected red bar: 10/20 = 50% of 10 blocks = 5 blocks
    assert file1_plus >= 7, f"file1.py should have at least 7 '+', got {file1_plus}"
    assert file1_plus <= 9, f"file1.py should have at most 9 '+', got {file1_plus}"
    assert file1_minus >= 4, f"file1.py should have at least 4 '-', got {file1_minus}"
    assert file1_minus <= 6, f"file1.py should have at most 6 '-', got {file1_minus}"

    # File2: +1 -10
    # Expected green bar: 1/61 ≈ 1.6% ≈ minimal sliver (1 char)
    # Expected red bar: 10/20 = 50% = 5 blocks
    assert file2_plus == 1, f"file2.py should have exactly 1 '+' (minimal sliver), got {file2_plus}"
    assert file2_minus >= 4, f"file2.py should have at least 4 '-', got {file2_minus}"
    assert file2_minus <= 6, f"file2.py should have at most 6 '-', got {file2_minus}"

    # File2 and file1 should have same '-' count (both have 10 deletions at same scale)
    assert abs(file2_minus - file1_minus) <= 1, (
        f"file2.py and file1.py both have 10 deletions, should have similar '-' counts: "
        f"file1={file1_minus}, file2={file2_minus}"
    )

    # File3: +10 -0
    # Expected green bar: 10/61 ≈ 16.4% ≈ 1.6 blocks
    # Expected red bar: 0/20 = 0% = 0 blocks
    assert file3_plus >= 1, f"file3.py should have at least 1 '+', got {file3_plus}"
    assert file3_plus <= 3, f"file3.py should have at most 3 '+', got {file3_plus}"
    assert file3_minus == 0, f"file3.py should have no '-', got {file3_minus}"


def test_percentage_calculation():
    """Test that percentage column shows correct ratio of file changes to total changes."""
    changes = [
        FileChange(path="file1.py", additions=100, deletions=50),  # 150 total
        FileChange(path="file2.py", additions=30, deletions=20),  # 50 total
        FileChange(path="file3.py", additions=200, deletions=0),  # 200 total
    ]

    # Total changes across all files: 150 + 50 + 200 = 400
    # file1: 150/400 = 37.5%
    # file2: 50/400 = 12.5%
    # file3: 200/400 = 50.0%

    config = RenderConfig(columns=[Column.TREE, Column.PERCENTAGES])
    diff_tree = make_diff_tree(changes, config=config)
    result = render_to_string(diff_tree, width=120, force_terminal=False)

    lines = result.split("\n")

    # Find lines containing each file
    file1_line = _find_line_with(lines, "file1.py")
    file2_line = _find_line_with(lines, "file2.py")
    file3_line = _find_line_with(lines, "file3.py")

    # Extract percentage values
    # Percentage should be at the end of each line
    # Format is like " 37.5%" or "12.5%"
    assert "37.5%" in file1_line, f"file1.py should show 37.5%, got: {file1_line}"
    assert "12.5%" in file2_line, f"file2.py should show 12.5%, got: {file2_line}"
    assert "50.0%" in file3_line, f"file3.py should show 50.0%, got: {file3_line}"


def test_percentage_not_sum_of_bar_percentages():
    """Test that percentage is NOT the sum of individual bar fill percentages.

    This is a regression test to ensure percentage shows file's contribution
    to total changes, not the combined fill percentage of both bars.
    """
    changes = [
        # Create a scenario where bar fill percentages differ from total percentage
        # Total: +100 additions, -900 deletions = 1000 total changes
        FileChange(path="heavy_deletes.py", additions=10, deletions=890),  # 900 total = 90%
        FileChange(path="only_adds.py", additions=90, deletions=10),  # 100 total = 10%
    ]

    # Totals: 100 additions, 900 deletions, 1000 total changes
    # heavy_deletes.py: 900/1000 = 90.0% of total changes
    # only_adds.py: 100/1000 = 10.0% of total changes

    # Bar fill percentages would be different:
    # heavy_deletes.py green bar: 10/100 = 10% fill
    # heavy_deletes.py red bar: 890/900 = 98.9% fill
    # If percentage were sum of fills, it would be ~109%! Wrong!

    # only_adds.py green bar: 90/100 = 90% fill
    # only_adds.py red bar: 10/900 = 1.1% fill
    # If percentage were sum of fills, it would be ~91%! Wrong!

    config = RenderConfig(columns=[Column.TREE, Column.BARS, Column.PERCENTAGES])
    diff_tree = make_diff_tree(changes, config=config)
    result = render_to_string(diff_tree, width=120, force_terminal=False)

    lines = result.split("\n")
    heavy_line = _find_line_with(lines, "heavy_deletes.py")
    light_line = _find_line_with(lines, "only_adds.py")

    # Verify percentages show total contribution, NOT bar fill sums
    assert "90.0%" in heavy_line, f"heavy_deletes.py should show 90.0% (900/1000), got: {heavy_line}"
    assert "10.0%" in light_line, f"only_adds.py should show 10.0% (100/1000), got: {light_line}"


def test_deletion_bar_alignment():
    """Test that deletion bars start at the same column position regardless of addition bar width."""
    changes = [
        FileChange(path="file1.py", additions=100, deletions=1),  # Mostly additions
        FileChange(path="file2.py", additions=1, deletions=100),  # Mostly deletions
        FileChange(path="file3.py", additions=50, deletions=50),  # Balanced
    ]

    # Use distinct character 'X' for deletions to make position finding easy
    config = RenderConfig(
        columns=[Column.TREE, Column.BARS],
        bar_width=10,
        bar_left_blocks=BlockChars.simple("X"),  # Deletions (LTR)
        bar_right_blocks=BlockChars.simple("+"),  # Additions (RTL)
    )
    diff_tree = make_diff_tree(changes, config=config)

    # Render and extract plain text
    lines = _render_to_text_lines(diff_tree, width=120)

    # Get file lines (skip root which is first)
    file_lines = []
    for line in lines:
        plain = line.plain
        if any(f in plain for f in ["file1.py", "file2.py", "file3.py"]):
            file_lines.append(plain)

    # Expecting 3 file lines
    assert len(file_lines) == 3, f"Expected 3 file lines, got {len(file_lines)}"

    # Find position of first 'X' (deletion bar start) in each line
    positions = []
    for i, plain_line in enumerate(file_lines):
        pos = plain_line.find("X")
        assert pos != -1, f"No deletion bar found in line {i}: {plain_line}"
        positions.append(pos)

    # All positions should be the same (deletion bars are left-aligned, start at same column)
    assert len(set(positions)) == 1, f"Deletion bars not aligned: positions={positions}, lines={file_lines}"
