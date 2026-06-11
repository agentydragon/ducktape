"""Snapshot tests for ANSI-rendered output."""

import pytest

from difftree.config import DEFAULT_CONFIG, Column, RenderConfig, SortMode
from difftree.conftest import make_diff_tree, render_to_string as render_renderable
from difftree.parser import FileChange


@pytest.fixture
def complex_changes() -> list[FileChange]:
    """Complex file changes for snapshot testing."""
    return [
        FileChange(path="src/main.py", additions=50, deletions=10),
        FileChange(path="src/utils.py", additions=30, deletions=5),
        FileChange(path="src/models/user.py", additions=100, deletions=20),
        FileChange(path="src/models/post.py", additions=80, deletions=15),
        FileChange(path="src/api/routes.py", additions=60, deletions=8),
        FileChange(path="tests/test_main.py", additions=40, deletions=2),
        FileChange(path="tests/test_models.py", additions=70, deletions=5),
        FileChange(path="README.md", additions=20, deletions=3),
        FileChange(path="docs/api.md", additions=25, deletions=0),
    ]


def render_to_string(
    changes: list[FileChange], sort_by: SortMode = SortMode.SIZE, config: RenderConfig | None = None
) -> str:
    """Helper to render tree to string."""
    diff_tree = make_diff_tree(changes, config=config, sort_by=sort_by)
    result = render_renderable(diff_tree, width=120, legacy_windows=False, color_system="standard")

    assert "…" not in result, "Output contains ellipsis character (…)"
    return result


@pytest.mark.parametrize(
    ("test_id", "sort_by", "config"),
    [
        ("default_rendering", SortMode.SIZE, None),
        ("alphabetical_sort", SortMode.ALPHA, None),
        ("no_bars", SortMode.SIZE, RenderConfig(columns=[Column.TREE, Column.COUNTS, Column.PERCENTAGES])),
        ("no_counts", SortMode.SIZE, RenderConfig(columns=[Column.TREE, Column.BARS, Column.PERCENTAGES])),
        ("no_percentages", SortMode.SIZE, RenderConfig(columns=[Column.TREE, Column.COUNTS, Column.BARS])),
        ("minimal", SortMode.SIZE, RenderConfig(columns=[Column.TREE])),
        ("custom_bar_width", SortMode.SIZE, RenderConfig(columns=DEFAULT_CONFIG.columns, bar_width=30)),
    ],
)
def test_snapshot_config_variants(snapshot, complex_changes, test_id, sort_by, config):
    """Snapshot test for different configuration variants."""
    output = render_to_string(complex_changes, sort_by=sort_by, config=config)
    assert output == snapshot(name=test_id)


@pytest.mark.parametrize(
    ("test_id", "changes"),
    [
        (
            "small_tree",
            [
                FileChange(path="main.py", additions=10, deletions=2),
                FileChange(path="utils.py", additions=5, deletions=1),
            ],
        ),
        (
            "deep_nesting",
            [
                FileChange(path="a/b/c/d/e/file.py", additions=20, deletions=5),
                FileChange(path="a/b/c/x/y/file.py", additions=15, deletions=3),
                FileChange(path="a/b/file.py", additions=10, deletions=2),
            ],
        ),
        (
            "only_additions",
            [
                FileChange(path="new_file1.py", additions=50, deletions=0),
                FileChange(path="new_file2.py", additions=30, deletions=0),
                FileChange(path="dir/new_file3.py", additions=20, deletions=0),
            ],
        ),
        (
            "only_deletions",
            [
                FileChange(path="old_file1.py", additions=0, deletions=50),
                FileChange(path="old_file2.py", additions=0, deletions=30),
                FileChange(path="dir/old_file3.py", additions=0, deletions=20),
            ],
        ),
        (
            "binary_file",
            [
                FileChange(path="src/code.py", additions=10, deletions=2),
                FileChange(path="assets/image.png", additions=0, deletions=0, is_binary=True),
                FileChange(path="assets/data.bin", additions=0, deletions=0, is_binary=True),
                FileChange(path="README.md", additions=5, deletions=1),
            ],
        ),
    ],
)
def test_snapshot_scenarios(snapshot, test_id, changes):
    output = render_to_string(changes)
    assert output == snapshot(name=test_id)
