"""Tests for configuration module."""

import pytest
import pytest_bazel

from difftree.config import Column, RenderConfig, parse_columns


@pytest.mark.parametrize(
    ("test_id", "input_str", "expected"),
    [
        ("valid", "tree,counts,bars,percentages", [Column.TREE, Column.COUNTS, Column.BARS, Column.PERCENTAGES]),
        ("case_insensitive", "TREE,CoUnTs,BaRs", [Column.TREE, Column.COUNTS, Column.BARS]),
        ("with_spaces", "tree, counts, bars", [Column.TREE, Column.COUNTS, Column.BARS]),
        ("single", "tree", [Column.TREE]),
    ],
)
def test_parse_columns(test_id, input_str, expected):
    """Test column parsing with various inputs."""
    result = parse_columns(input_str)
    assert result == expected


def test_parse_columns_invalid():
    """Test parsing invalid column name raises ValueError."""
    with pytest.raises(ValueError, match="Unknown column 'invalid'"):
        parse_columns("tree,invalid,counts")


def test_render_config_minimal():
    """Test minimal RenderConfig."""
    config = RenderConfig(columns=[Column.TREE])
    assert config.columns == [Column.TREE]


if __name__ == "__main__":
    pytest_bazel.main()
