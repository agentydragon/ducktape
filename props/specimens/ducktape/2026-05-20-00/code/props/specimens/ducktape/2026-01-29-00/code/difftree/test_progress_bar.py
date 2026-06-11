"""Tests for ProgressBar component."""

import pytest
import pytest_bazel

from difftree.conftest import LEFT_BLOCK_CHARS, RIGHT_BLOCK_CHARS, render_to_string
from difftree.progress_bar import DEFAULT_LEFT_BLOCKS, DEFAULT_RIGHT_BLOCKS, ProgressBar


def render_bar(bar: ProgressBar, width: int) -> str:
    """Test helper to render a progress bar at a specific width."""
    return render_to_string(bar, width=width, force_terminal=False, color_system=None).rstrip("\n")


def test_progress_bar_empty():
    """Test progress bar with 0 value maintains correct width."""
    bar = ProgressBar(0, 100, DEFAULT_LEFT_BLOCKS, "left", "green")
    rendered = render_bar(bar, 10)
    assert len(rendered) == 10
    assert rendered.strip() == ""


def test_progress_bar_full():
    """Test progress bar at 100% shows full block character."""
    bar = ProgressBar(100, 100, DEFAULT_LEFT_BLOCKS, "left", "green")
    rendered = render_bar(bar, 10)
    assert "█" in rendered
    assert len(rendered) == 10


@pytest.mark.parametrize(
    ("value", "max_value", "expected_blocks"),
    [
        (50, 100, 5),  # Exactly half
        (25, 100, 2),  # Quarter (approx 2-3 blocks)
        (75, 100, 7),  # Three quarters (approx 7-8 blocks)
    ],
)
def test_progress_bar_partial(value, max_value, expected_blocks):
    """Test progress bar with partial fill shows proportional blocks."""
    bar = ProgressBar(value, max_value, DEFAULT_LEFT_BLOCKS, "left", "green")
    rendered = render_bar(bar, 10)
    plain = rendered.strip()
    # Should have some filled blocks (allow +/- 1 for partial blocks)
    assert len(plain) >= expected_blocks - 1
    assert len(plain) <= expected_blocks + 1
    assert plain != ""


def test_progress_bar_right_aligned():
    """Test right-aligned progress bar has padding on the left."""
    bar = ProgressBar(30, 100, DEFAULT_RIGHT_BLOCKS, "right", "green")
    rendered = render_bar(bar, 10)
    plain = rendered
    # Should be right-aligned (ends with filled blocks, padding on left)
    assert plain.endswith(RIGHT_BLOCK_CHARS) or plain.strip() == ""
    assert len(plain) == 10


def test_progress_bar_left_aligned():
    """Test left-aligned progress bar has padding on the right."""
    bar = ProgressBar(30, 100, DEFAULT_LEFT_BLOCKS, "left", "green")
    rendered = render_bar(bar, 10)
    plain = rendered
    # Should be left-aligned (starts with filled blocks, padding on right)
    assert len(plain) == 10
    assert plain.startswith(LEFT_BLOCK_CHARS) or plain.strip() == ""


@pytest.mark.parametrize(
    ("value", "max_value", "expected_has_sliver"),
    [
        (1, 10000, True),  # Very small ratio
        (1, 1000000, True),  # Extremely small ratio
        (1, 100, True),  # Small but visible ratio
        (0, 100, False),  # Zero should show nothing
    ],
)
def test_minimum_sliver(value, max_value, expected_has_sliver):
    """Test that any value >0 shows at least a minimal sliver."""
    bar = ProgressBar(value, max_value, DEFAULT_LEFT_BLOCKS, "left", "green")
    rendered = render_bar(bar, 20)
    plain = rendered

    assert len(plain) == 20

    if expected_has_sliver:
        # Should have at least the thinnest partial block
        assert any(block in plain for block in DEFAULT_LEFT_BLOCKS.partials)
    else:
        # Should be all spaces
        assert plain.strip() == ""


@pytest.mark.parametrize("align", ["left", "right"])
def test_minimum_sliver_alignment(align):
    """Test minimum sliver works with both alignments."""
    blocks = DEFAULT_LEFT_BLOCKS if align == "left" else DEFAULT_RIGHT_BLOCKS
    bar = ProgressBar(1, 10000, blocks, align, "green")
    rendered = render_bar(bar, 20)
    plain = rendered

    # Should have appropriate block character based on alignment
    if align == "left":
        assert "▏" in plain  # Left-growing block for LTR
    else:
        assert "▕" in plain  # Right-growing block for RTL
    assert len(plain) == 20


if __name__ == "__main__":
    pytest_bazel.main()
