"""Tests for tree renderer."""

from io import StringIO

from git_diff_tree.parser import FileChange
from git_diff_tree.renderer import BLOCKS, DiffTreeRenderer
from git_diff_tree.tree import build_tree
import pytest
from rich.console import Console


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
    renderer = DiffTreeRenderer(console=console)
    renderer.render(root, max_depth=1)

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
