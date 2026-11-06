"""Tests for DiffTree renderable."""

import re
from dataclasses import replace

import pytest
import pytest_bazel

from difftree.config import DEFAULT_CONFIG, Column, RenderConfig
from difftree.conftest import make_diff_tree, render_to_string
from difftree.parser import FileChange
from difftree.progress_bar import BlockChars


def _find_line_with(lines: list[str], substring: str) -> str:
    """Find first line containing substring."""
    return next(line for line in lines if substring in line)


def _find_tree_pos(line: str) -> int:
    """Find position of first tree decoration character, or -1 if not found."""
    positions = [line.find(char) for char in [".", "└", "├", "│"] if char in line]
    return min(positions) if positions else -1


def test_render_with_max_depth(sample_changes):
    """Test rendering with maximum depth limit."""
    config = replace(DEFAULT_CONFIG, max_depth=1)
    diff_tree = make_diff_tree(sample_changes, config=config)
    result = render_to_string(diff_tree, width=120)

    # Top-level entries render; children beyond the depth limit do not.
    assert "src" in result
    assert "main.py" not in result


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
        assert "+100" in result

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


@pytest.mark.parametrize(
    ("columns", "first", "second"),
    [
        ([Column.BARS, Column.TREE, Column.COUNTS], "bars", "tree"),
        ([Column.TREE, Column.BARS, Column.COUNTS], "tree", "bars"),
        ([Column.COUNTS, Column.TREE], "counts", "tree"),
    ],
)
def test_column_ordering(columns, first, second):
    changes = [FileChange(path="file.py", additions=10, deletions=5)]
    config = RenderConfig(columns=columns)
    result = render_to_string(make_diff_tree(changes, config=config), width=80, force_terminal=False)
    line = _find_line_with(result.split("\n"), "file.py")
    positions = {"tree": _find_tree_pos(line), "bars": line.find("█"), "counts": line.find("+10")}
    assert 0 <= positions[first] < positions[second]


@pytest.mark.parametrize(
    ("columns", "present", "absent"),
    [
        ([Column.TREE], (), ("+7", "-2", "A", "D", "%")),
        ([Column.TREE, Column.COUNTS], ("+7", "-2"), ("A", "D", "%")),
        ([Column.TREE, Column.BARS], ("A", "D"), ("+7", "-2", "%")),
        ([Column.TREE, Column.PERCENTAGES], ("100.0%",), ("+7", "-2", "A", "D")),
    ],
)
def test_configured_columns_control_rendered_content(columns, present, absent):
    changes = [FileChange(path="src/file.py", additions=7, deletions=2)]
    config = RenderConfig(
        columns=columns, bar_left_blocks=BlockChars.simple("D"), bar_right_blocks=BlockChars.simple("A")
    )

    result = render_to_string(
        make_diff_tree(changes, config=config), width=120, force_terminal=False, color_system=None
    )

    assert "src/file.py" in result
    for content in present:
        assert content in result
    for content in absent:
        assert content not in result


def test_configured_bar_width_bounds_each_bar():
    changes = [FileChange(path="src/file.py", additions=10, deletions=10)]
    config = RenderConfig(
        columns=[Column.TREE, Column.BARS],
        bar_width=8,
        bar_left_blocks=BlockChars.simple("D"),
        bar_right_blocks=BlockChars.simple("A"),
    )

    result = render_to_string(
        make_diff_tree(changes, config=config), width=120, force_terminal=False, color_system=None
    )
    file_line = _find_line_with(result.splitlines(), "src/file.py")

    assert file_line.count("A") == config.bar_width
    assert file_line.count("D") == config.bar_width


def test_nested_paths_are_collapsed_and_binary_files_are_labeled():
    changes = [
        FileChange(path="src/generated/module.py", additions=3, deletions=1),
        FileChange(path="assets/image.png", additions=0, deletions=0, is_binary=True),
        FileChange(path="docs/removed.md", additions=0, deletions=4),
    ]

    result = render_to_string(make_diff_tree(changes), width=120, force_terminal=False, color_system=None)
    lines = result.splitlines()
    binary_line = _find_line_with(lines, "assets/image.png")
    changed_line = _find_line_with(lines, "src/generated/module.py")
    deleted_line = _find_line_with(lines, "docs/removed.md")

    assert "src/generated/module.py" in result
    assert "[Binary]" in binary_line
    assert not any(block in binary_line for block in "█▏▕▎▍▌▋▊▉")
    assert "+3" in changed_line
    assert "-1" in changed_line
    assert "-4" in deleted_line


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

    bar_count_bounds = {"file1.py": ((7, 9), (4, 6)), "file2.py": ((1, 1), (4, 6)), "file3.py": ((1, 3), (0, 0))}

    # Render and extract plain text (no ANSI codes needed)
    lines = render_to_string(diff_tree, width=80, force_terminal=False, color_system=None).splitlines()

    file_lines = {}
    for line in lines:
        for file_path in bar_count_bounds:
            if file_path in line:
                file_lines[file_path] = line
                break

    bar_counts = {file_path: (line.count("+"), line.count("-")) for file_path, line in file_lines.items()}
    for file_path, (plus_bounds, minus_bounds) in bar_count_bounds.items():
        plus_count, minus_count = bar_counts[file_path]
        assert plus_bounds[0] <= plus_count <= plus_bounds[1], (
            f"{file_path} '+' count outside {plus_bounds}: {plus_count}"
        )
        assert minus_bounds[0] <= minus_count <= minus_bounds[1], (
            f"{file_path} '-' count outside {minus_bounds}: {minus_count}"
        )

    assert abs(bar_counts["file2.py"][1] - bar_counts["file1.py"][1]) <= 1


def test_percentage_calculation():
    """Test that percentage column shows correct ratio of file changes to total changes."""
    changes = [
        FileChange(path="file1.py", additions=100, deletions=50),
        FileChange(path="file2.py", additions=30, deletions=20),
        FileChange(path="file3.py", additions=200, deletions=0),
    ]

    config = RenderConfig(columns=[Column.TREE, Column.PERCENTAGES])
    diff_tree = make_diff_tree(changes, config=config)
    result = render_to_string(diff_tree, width=120, force_terminal=False)

    lines = result.split("\n")
    expected_percentages = {"file1.py": "37.5%", "file2.py": "12.5%", "file3.py": "50.0%"}
    for file_path, expected in expected_percentages.items():
        line = _find_line_with(lines, file_path)
        assert expected in line, f"{file_path} should show {expected}, got: {line}"


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
    lines = render_to_string(diff_tree, width=120, force_terminal=False, color_system=None).splitlines()

    # Get file lines (skip root which is first)
    file_lines = [line for line in lines if any(f in line for f in ["file1.py", "file2.py", "file3.py"])]

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


if __name__ == "__main__":
    pytest_bazel.main()
