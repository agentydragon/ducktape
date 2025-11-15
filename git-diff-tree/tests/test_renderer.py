"""Tests for tree renderer."""

from io import StringIO

from git_diff_tree.config import RenderConfig
from git_diff_tree.parser import FileChange
from git_diff_tree.renderer import BLOCKS, DiffTreeRenderer
from git_diff_tree.tree import build_tree
import pytest
from rich.console import Console
from rich.text import Text


def test_blocks_constant():
    """Test that BLOCKS contains the expected Unicode characters."""
    assert len(BLOCKS) == 9
    assert BLOCKS[0] == " "
    assert BLOCKS[-1] == "█"


def test_renderer_initialization():
    """Test DiffTreeRenderer initialization."""
    from git_diff_tree.config import Column

    renderer = DiffTreeRenderer()

    assert renderer.console is not None
    assert Column.COUNTS in renderer.config.columns
    assert Column.BARS in renderer.config.columns
    assert Column.PERCENTAGES in renderer.config.columns
    assert renderer.config.bar_width == 20


def test_renderer_with_custom_options():
    """Test DiffTreeRenderer with custom options."""
    from git_diff_tree.config import Column, RenderConfig

    console = Console()
    config = RenderConfig(columns=[Column.TREE], bar_width=30)
    renderer = DiffTreeRenderer(console=console, config=config)

    assert renderer.console is console
    assert Column.COUNTS not in renderer.config.columns
    assert Column.BARS not in renderer.config.columns
    assert Column.PERCENTAGES not in renderer.config.columns
    assert renderer.config.bar_width == 30


def test_render_simple_tree(sample_changes):
    """Test rendering a simple tree structure."""
    # Create a string buffer to capture output
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=120)

    root = build_tree(sample_changes)
    renderer = DiffTreeRenderer(console=console)
    renderer.render(root)

    # Get the output
    result = output.getvalue()

    # Check that key elements are present
    assert "src" in result
    assert "tests" in result
    assert "README.md" in result
    assert "main.py" in result
    assert "models" in result


def test_render_with_no_counts(sample_changes):
    """Test rendering without count columns."""
    from git_diff_tree.config import Column, RenderConfig

    output = StringIO()
    console = Console(file=output, force_terminal=True, width=120)

    root = build_tree(sample_changes)
    config = RenderConfig(columns=[Column.TREE, Column.BARS, Column.PERCENTAGES])
    renderer = DiffTreeRenderer(console=console, config=config)
    renderer.render(root)

    result = output.getvalue()

    # Should still have tree structure but different formatting
    assert "src" in result


def test_render_with_max_depth(sample_changes):
    """Test rendering with maximum depth limit."""
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=120)

    root = build_tree(sample_changes)
    config = RenderConfig.default()
    config.max_depth = 1
    renderer = DiffTreeRenderer(console=console, config=config)
    renderer.render(root)

    result = output.getvalue()

    # Should show top-level items but not deeply nested ones
    assert "src" in result
    # Depth limit might prevent showing nested files
    # This is a simplified test


def test_make_progress_bar():
    """Test progress bar generation."""
    from git_diff_tree.config import RenderConfig

    config = RenderConfig.default()
    config.bar_width = 10
    renderer = DiffTreeRenderer(config=config)

    # Test empty bar
    bar = renderer._make_progress_bar(0, 100, 10, "left", "green")
    assert len(bar.plain) == 10

    # Test full bar
    bar = renderer._make_progress_bar(100, 100, 10, "left", "green")
    assert "█" in bar.plain

    # Test partial bar
    bar = renderer._make_progress_bar(50, 100, 10, "left", "green")
    # Should have some filled blocks
    assert bar.plain.strip() != ""


def test_make_progress_bar_alignment():
    """Test progress bar alignment."""
    from git_diff_tree.config import RenderConfig

    config = RenderConfig.default()
    config.bar_width = 10
    renderer = DiffTreeRenderer(config=config)

    # Right-aligned bar
    bar_right = renderer._make_progress_bar(30, 100, 10, "right", "green")
    plain = bar_right.plain
    # Should be right-aligned (padding on left)
    assert (
        plain.endswith(("█", "▉", "▊", "▋", "▌", "▍", "▎", "▏")) or plain.strip() == ""
    )

    # Left-aligned bar
    bar_left = renderer._make_progress_bar(30, 100, 10, "left", "green")
    plain = bar_left.plain
    # Should be left-aligned (padding on right)
    assert len(plain) == 10


@pytest.mark.parametrize(
    ("value", "max_value", "expected_has_sliver"),
    [
        (1, 10000, True),  # Very small ratio
        (1, 1000000, True),  # Extremely small ratio
        (1, 100, True),  # Small but visible ratio
        (0, 100, False),  # Zero should show nothing
    ],
)
def test_make_progress_bar_minimum_sliver(value, max_value, expected_has_sliver):
    """Test that any value >0 shows at least a minimal sliver."""
    from git_diff_tree.config import RenderConfig

    config = RenderConfig.default()
    config.bar_width = 20
    renderer = DiffTreeRenderer(config=config)

    bar = renderer._make_progress_bar(value, max_value, 20, "left", "green")
    plain = bar.plain

    assert len(plain) == 20

    if expected_has_sliver:
        # Should have at least ▏
        assert "▏" in plain or any(block in plain for block in BLOCKS[1:])
    else:
        # Should be all spaces
        assert plain.strip() == ""


@pytest.mark.parametrize("align", ["left", "right"])
def test_make_progress_bar_minimum_sliver_alignment(align):
    """Test minimum sliver works with both alignments."""
    from git_diff_tree.config import RenderConfig

    config = RenderConfig.default()
    config.bar_width = 20
    renderer = DiffTreeRenderer(config=config)

    bar = renderer._make_progress_bar(1, 10000, 20, align, "green")
    plain = bar.plain

    # Should have ▏ regardless of alignment
    assert "▏" in plain
    assert len(plain) == 20


def test_minimum_sliver_with_small_changes():
    """Test rendering with one very small change among larger ones."""
    # Create changes where one is very small relative to others
    changes = [
        FileChange(path="large_file.py", additions=10000, deletions=5000),
        FileChange(path="tiny_file.py", additions=1, deletions=0),
    ]

    output = StringIO()
    console = Console(file=output, force_terminal=True, width=120)

    root = build_tree(changes)
    renderer = DiffTreeRenderer(console=console)
    renderer.render(root)

    result = output.getvalue()

    # Both files should be visible in the output
    assert "large_file.py" in result
    assert "tiny_file.py" in result
    # The tiny file should have some visible indicator despite small ratio
    # (This is a high-level test; the unit test above is more precise)


# Console width tests


@pytest.mark.parametrize(
    ("width", "description"),
    [
        (40, "too_narrow"),  # Very narrow terminal
        (80, "just_right"),  # Standard terminal width
        (200, "very_wide"),  # Wide terminal
    ],
)
def test_console_width_handling(width, description):
    """Test rendering with different console widths."""
    from git_diff_tree.config import RenderConfig

    changes = [
        FileChange(
            path="src/very_long_filename_that_might_wrap.py",
            additions=100,
            deletions=50,
        ),
        FileChange(path="test.py", additions=10, deletions=5),
    ]

    output = StringIO()
    console = Console(file=output, force_terminal=True, width=width)

    root = build_tree(changes)
    config = RenderConfig.default()
    renderer = DiffTreeRenderer(console=console, config=config)
    renderer.render(root)

    result = output.getvalue()

    # Basic assertions: output should contain expected elements
    assert result.strip() != ""

    # File names should appear (possibly truncated for narrow widths)
    assert "test.py" in result

    # Stats should be present
    assert "+100" in result or "+10" in result

    # For wide consoles, check full filename visibility
    if width >= 80:
        assert "very_long_filename" in result


# Progress bar format tests


def _extract_progress_bars(line: str) -> str:
    """Extract just the progress bar characters from a line (after filename and counts)."""
    # Strip ANSI codes first
    plain = Text.from_ansi(line).plain

    # Find the progress bar part - it's the block characters between counts and percentage
    # Block characters are: " ▏▎▍▌▋▊▉█"
    block_chars = " ▏▎▍▌▋▊▉█"
    in_blocks = False
    blocks_part = []

    for char in plain:
        if char in block_chars:
            in_blocks = True
            blocks_part.append(char)
        elif in_blocks and char not in block_chars:
            # Reached the end of blocks section
            break

    return "".join(blocks_part)


def test_progress_bar_format_pattern():
    """Test that progress bars match the expected format: green(RTL) + red(LTR) with no space."""
    changes = [
        FileChange(path="file.py", additions=100, deletions=50),
    ]

    output = StringIO()
    console = Console(file=output, force_terminal=True, width=120, legacy_windows=False)

    root = build_tree(changes)
    config = RenderConfig.default()
    config.bar_width = 20  # Set explicit width for predictability
    renderer = DiffTreeRenderer(console=console, config=config)
    renderer.render(root)

    result = output.getvalue()

    # Extract the line with file.py
    lines = result.split("\n")
    file_line = next(line for line in lines if "file.py" in line)

    # Extract just the progress bar part
    bars = _extract_progress_bars(file_line)

    # The pattern should be:
    # - Some spaces + optional partial block + full blocks (green, right-aligned)
    # - Full blocks + optional partial block + spaces (red, left-aligned)
    # Total length should be 2 * bar_width (20 + 20 = 40)

    assert len(bars) == 40, f"Expected 40 chars, got {len(bars)}: {bars!r}"

    # Check format: green part (0-19) and red part (20-39) touch with no space
    green_part = bars[:20]
    red_part = bars[20:40]

    # Green part: right-aligned (spaces on left, blocks on right)
    # Should end with a block character (not space) if there are additions
    assert green_part.rstrip(" ") != "", "Green part should have some blocks"
    assert green_part[-1] != " ", "Green part should end with a block, not space"

    # Red part: left-aligned (blocks on left, spaces on right)
    # Should start with a block character (not space) if there are deletions
    assert red_part.lstrip(" ") != "", "Red part should have some blocks"
    assert red_part[0] != " ", "Red part should start with a block, not space"

    # No space between green and red
    # (already ensured by the alignment checks above - green ends with block, red starts with block)


@pytest.mark.parametrize(
    ("additions", "deletions"),
    [
        (100, 50),
        (200, 10),
        (5, 300),
        (1000, 500),
        (1, 1),
    ],
)
def test_progress_bar_format_various_sizes(additions, deletions):
    """Test progress bar format with files of different sizes."""
    changes = [
        FileChange(
            path=f"file_{additions}_{deletions}.py",
            additions=additions,
            deletions=deletions,
        ),
    ]

    output = StringIO()
    console = Console(file=output, force_terminal=True, width=150, legacy_windows=False)

    root = build_tree(changes)
    config = RenderConfig.default()
    config.bar_width = 20
    renderer = DiffTreeRenderer(console=console, config=config)
    renderer.render(root)

    result = output.getvalue()
    lines = result.split("\n")
    file_line = next(
        line for line in lines if f"file_{additions}_{deletions}.py" in line
    )

    bars = _extract_progress_bars(file_line)

    # Check total length
    assert len(bars) == 40, (
        f"Expected 40 chars for {additions}+/{deletions}-, got {len(bars)}"
    )

    green_part = bars[:20]
    red_part = bars[20:40]

    # If there are additions, green part should end with a block
    if additions > 0:
        assert green_part.rstrip(" ") != "", (
            f"Green part empty for {additions} additions"
        )
        assert green_part[-1] != " ", (
            f"Green part ends with space for {additions} additions"
        )

    # If there are deletions, red part should start with a block
    if deletions > 0:
        assert red_part.lstrip(" ") != "", f"Red part empty for {deletions} deletions"
        assert red_part[0] != " ", (
            f"Red part starts with space for {deletions} deletions"
        )


def test_progress_bars_align_consistently():
    """Test that files with same delta count have progress bars at same position."""
    # 3 files with same additions and deletions but different names
    changes = [
        FileChange(path="aaaa.py", additions=100, deletions=50),
        FileChange(path="bbbbbbbbbb.py", additions=100, deletions=50),
        FileChange(path="c.py", additions=100, deletions=50),
    ]

    output = StringIO()
    console = Console(file=output, force_terminal=True, width=150, legacy_windows=False)

    root = build_tree(changes)
    config = RenderConfig.default()
    config.bar_width = 20
    renderer = DiffTreeRenderer(console=console, config=config)
    renderer.render(root)

    result = output.getvalue()
    lines = [Text.from_ansi(line).plain for line in result.split("\n")]

    # Extract lines for each file
    file_lines = {
        "aaaa.py": next(line for line in lines if "aaaa.py" in line),
        "bbbbbbbbbb.py": next(line for line in lines if "bbbbbbbbbb.py" in line),
        "c.py": next(line for line in lines if "c.py" in line),
    }

    # Find the character range where progress bars appear in each line
    # Progress bars are the consecutive block characters
    block_chars = set(" ▏▎▍▌▋▊▉█")

    def find_bar_range(line: str) -> tuple[int, int]:
        """Find start and end index of progress bar section."""
        start = None
        end = None
        in_blocks = False
        consecutive_blocks = 0

        for i, char in enumerate(line):
            if char in block_chars:
                if not in_blocks:
                    # Count consecutive block chars to distinguish from single spaces
                    in_blocks = True
                    start = i
                consecutive_blocks += 1
            else:
                if (
                    in_blocks and consecutive_blocks >= 10
                ):  # Must be substantial to be the bar
                    end = i
                    break
                in_blocks = False
                consecutive_blocks = 0

        return (start or -1, end or -1)

    ranges = {name: find_bar_range(line) for name, line in file_lines.items()}

    # All files should have bars starting and ending at the same position
    # (since they have the same stats and we're in a consistent layout)
    start_positions = [r[0] for r in ranges.values()]
    end_positions = [r[1] for r in ranges.values()]

    # The bars should align at the same column positions
    assert len(set(start_positions)) == 1, f"Bar start positions don't align: {ranges}"
    assert len(set(end_positions)) == 1, f"Bar end positions don't align: {ranges}"
