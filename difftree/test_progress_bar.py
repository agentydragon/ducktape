"""Tests for ProgressBar component."""

import pytest
import pytest_bazel

from difftree.conftest import RIGHT_BLOCK_CHARS, render_to_string
from difftree.progress_bar import DEFAULT_LEFT_BLOCKS, DEFAULT_RIGHT_BLOCKS, ProgressBar


def render_bar(bar: ProgressBar, width: int) -> str:
    """Test helper to render a progress bar at a specific width."""
    return render_to_string(bar, width=width, force_terminal=False, color_system=None).rstrip("\n")


def test_progress_bar_right_aligned():
    """Test right-aligned progress bar has padding on the left."""
    bar = ProgressBar(30, 100, DEFAULT_RIGHT_BLOCKS, "right", "green")
    rendered = render_bar(bar, 10)
    plain = rendered
    # Should be right-aligned (ends with filled blocks, padding on left)
    assert plain.endswith(RIGHT_BLOCK_CHARS)
    assert len(plain) == 10


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
