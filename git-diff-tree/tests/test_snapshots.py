"""Snapshot tests for ANSI-rendered output."""

from io import StringIO
from typing import Optional

from git_diff_tree.config import Column, RenderConfig
from git_diff_tree.parser import FileChange
from git_diff_tree.renderer import DiffTreeRenderer
from git_diff_tree.tree import build_tree, sort_tree
import pytest
from rich.console import Console


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
    changes: list[FileChange],
    sort_by: str = "size",
    config: Optional[RenderConfig] = None,
) -> str:
    """Helper to render tree to string."""
    output = StringIO()
    console = Console(
        file=output,
        force_terminal=True,
        width=120,
        legacy_windows=False,
        color_system="standard",
    )

    root = build_tree(changes)
    sort_tree(root, sort_by=sort_by)

    renderer = DiffTreeRenderer(console=console, config=config)
    renderer.render(root)

    return output.getvalue()


def test_snapshot_default_rendering(snapshot, complex_changes):
    """Snapshot test for default rendering with all columns."""
    output = render_to_string(complex_changes)
    assert output == snapshot


def test_snapshot_alphabetical_sort(snapshot, complex_changes):
    """Snapshot test for alphabetical sorting."""
    output = render_to_string(complex_changes, sort_by="alpha")
    assert output == snapshot


def test_snapshot_no_bars(snapshot, complex_changes):
    """Snapshot test without progress bars."""
    config = RenderConfig(columns=[Column.TREE, Column.COUNTS, Column.PERCENTAGES])
    output = render_to_string(complex_changes, config=config)
    assert output == snapshot


def test_snapshot_no_counts(snapshot, complex_changes):
    """Snapshot test without count columns."""
    config = RenderConfig(columns=[Column.TREE, Column.BARS, Column.PERCENTAGES])
    output = render_to_string(complex_changes, config=config)
    assert output == snapshot


def test_snapshot_no_percentages(snapshot, complex_changes):
    """Snapshot test without percentage column."""
    config = RenderConfig(columns=[Column.TREE, Column.COUNTS, Column.BARS])
    output = render_to_string(complex_changes, config=config)
    assert output == snapshot


def test_snapshot_minimal(snapshot, complex_changes):
    """Snapshot test with minimal output (tree only)."""
    config = RenderConfig.minimal()
    output = render_to_string(complex_changes, config=config)
    assert output == snapshot


def test_snapshot_custom_bar_width(snapshot, complex_changes):
    """Snapshot test with custom progress bar width."""
    config = RenderConfig.default()
    config.bar_width = 30
    output = render_to_string(complex_changes, config=config)
    assert output == snapshot


def test_snapshot_small_tree(snapshot):
    """Snapshot test with a small tree."""
    changes = [
        FileChange(path="main.py", additions=10, deletions=2),
        FileChange(path="utils.py", additions=5, deletions=1),
    ]
    output = render_to_string(changes)
    assert output == snapshot


def test_snapshot_deep_nesting(snapshot):
    """Snapshot test with deeply nested structure."""
    changes = [
        FileChange(path="a/b/c/d/e/file.py", additions=20, deletions=5),
        FileChange(path="a/b/c/x/y/file.py", additions=15, deletions=3),
        FileChange(path="a/b/file.py", additions=10, deletions=2),
    ]
    output = render_to_string(changes)
    assert output == snapshot


def test_snapshot_only_additions(snapshot):
    """Snapshot test with only additions (no deletions)."""
    changes = [
        FileChange(path="new_file1.py", additions=50, deletions=0),
        FileChange(path="new_file2.py", additions=30, deletions=0),
        FileChange(path="dir/new_file3.py", additions=20, deletions=0),
    ]
    output = render_to_string(changes)
    assert output == snapshot


def test_snapshot_only_deletions(snapshot):
    """Snapshot test with only deletions (no additions)."""
    changes = [
        FileChange(path="old_file1.py", additions=0, deletions=50),
        FileChange(path="old_file2.py", additions=0, deletions=30),
        FileChange(path="dir/old_file3.py", additions=0, deletions=20),
    ]
    output = render_to_string(changes)
    assert output == snapshot
