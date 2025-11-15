"""Tests for configuration module."""

from git_diff_tree.config import Column, RenderConfig, parse_columns
import pytest


def test_parse_columns_valid():
    """Test parsing valid column names."""
    result = parse_columns("tree,counts,bars,percentages")
    assert result == [Column.TREE, Column.COUNTS, Column.BARS, Column.PERCENTAGES]


def test_parse_columns_case_insensitive():
    """Test column parsing is case-insensitive."""
    result = parse_columns("TREE,CoUnTs,BaRs")
    assert result == [Column.TREE, Column.COUNTS, Column.BARS]


def test_parse_columns_with_spaces():
    """Test column parsing handles spaces."""
    result = parse_columns("tree, counts, bars")
    assert result == [Column.TREE, Column.COUNTS, Column.BARS]


def test_parse_columns_invalid():
    """Test parsing invalid column name raises ValueError."""
    with pytest.raises(ValueError, match="Unknown column 'invalid'"):
        parse_columns("tree,invalid,counts")


def test_parse_columns_single():
    """Test parsing single column."""
    result = parse_columns("tree")
    assert result == [Column.TREE]


def test_render_config_default():
    """Test default RenderConfig."""
    config = RenderConfig.default()
    assert Column.TREE in config.columns
    assert Column.COUNTS in config.columns
    assert Column.BARS in config.columns
    assert Column.PERCENTAGES in config.columns
    assert config.bar_width == 20


def test_render_config_minimal():
    """Test minimal RenderConfig."""
    config = RenderConfig.minimal()
    assert config.columns == [Column.TREE]
