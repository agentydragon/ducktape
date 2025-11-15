"""Tests for tree renderer."""

from io import StringIO

import pytest
from rich.console import Console

from git_diff_tree.renderer import BLOCKS, DiffTreeRenderer
from git_diff_tree.tree import build_tree


def test_blocks_constant():
    """Test that BLOCKS contains the expected Unicode characters."""
    assert len(BLOCKS) == 9
    assert BLOCKS[0] == " "
    assert BLOCKS[-1] == "█"


def test_renderer_initialization():
    """Test DiffTreeRenderer initialization."""
    renderer = DiffTreeRenderer()

    assert renderer.console is not None
    assert renderer.show_counts is True
    assert renderer.show_bars is True
    assert renderer.show_percentages is True
    assert renderer.bar_width == 20


def test_renderer_with_custom_options():
    """Test DiffTreeRenderer with custom options."""
    console = Console()
    renderer = DiffTreeRenderer(
        console=console,
        show_counts=False,
        show_bars=False,
        show_percentages=False,
        bar_width=30,
    )

    assert renderer.console is console
    assert renderer.show_counts is False
    assert renderer.show_bars is False
    assert renderer.show_percentages is False
    assert renderer.bar_width == 30


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
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=120)

    root = build_tree(sample_changes)
    renderer = DiffTreeRenderer(console=console, show_counts=False)
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
    renderer = DiffTreeRenderer(bar_width=10)

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
    renderer = DiffTreeRenderer(bar_width=10)

    # Right-aligned bar
    bar_right = renderer._make_progress_bar(30, 100, 10, "right", "green")
    plain = bar_right.plain
    # Should be right-aligned (padding on left)
    assert plain.endswith(("█", "▉", "▊", "▋", "▌", "▍", "▎", "▏")) or plain.strip() == ""

    # Left-aligned bar
    bar_left = renderer._make_progress_bar(30, 100, 10, "left", "green")
    plain = bar_left.plain
    # Should be left-aligned (padding on right)
    assert len(plain) == 10
