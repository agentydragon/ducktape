"""CLI entry point for difftree."""

import os
import select
import stat
import sys

import click
from rich.console import Console

from difftree.config import Column, RenderConfig, SortMode, parse_columns
from difftree.diff_tree import DiffTree
from difftree.parser import parse_git_diff, parse_unified_diff
from difftree.tree import build_tree, sort_tree


def validate_columns(ctx, param, value):
    """Click callback to validate and parse column configuration."""
    try:
        return parse_columns(value)
    except ValueError as e:
        raise click.BadParameter(str(e))


@click.command()
@click.argument("diff_args", nargs=-1)
@click.option(
    "--sort", type=click.Choice(["size", "alpha"]), default="size", help="Sort mode: 'size' (default) or 'alpha'"
)
@click.option(
    "--columns",
    type=str,
    default="tree,counts,bars,percentages",
    callback=validate_columns,
    help="Columns to display (comma-separated): tree,counts,bars,percentages",
)
@click.option("--bar-width", type=int, default=20, help="Width of each progress bar (default: 20)")
@click.option("--max-depth", type=int, default=None, help="Maximum tree depth to display")
def main(diff_args: tuple[str, ...], sort: str, columns: list[Column], bar_width: int, max_depth: int | None) -> None:
    """
    Visualize diffs as a tree with progress bars.

    Can read from stdin (piped input) or run git diff directly.

    Examples:

    \b
    # Show unstaged changes
    difftree

    \b
    # Show changes between commits
    difftree HEAD~1 HEAD

    \b
    # Show staged changes
    difftree --cached

    \b
    # Use as a pager (read from stdin)
    git diff | difftree
    svn diff | difftree

    \b
    # Sort alphabetically and show only tree and counts
    difftree --sort alpha --columns tree,counts

    \b
    # Minimal output (tree structure only)
    difftree --columns tree
    """
    try:
        # Convert sort mode to enum
        sort_mode = SortMode(sort)

        # Check if stdin has actual data (not just EOF from capture_output=True)
        has_stdin_data = False
        if not sys.stdin.isatty():
            # Try to peek at stdin to see if there's real data
            try:
                # Use os.fstat to check if stdin is a regular file or pipe with data
                mode = os.fstat(sys.stdin.fileno()).st_mode
                if (stat.S_ISFIFO(mode) or stat.S_ISREG(mode)) and select.select([sys.stdin], [], [], 0.0)[0]:
                    # Data is available, but it might just be EOF
                    # Try to peek at one byte (BufferedReader has peek, but type system doesn't know)
                    peek_data = sys.stdin.buffer.peek(1)  # type: ignore[union-attr]
                    has_stdin_data = len(peek_data) > 0
            except (OSError, AttributeError):
                pass

        if has_stdin_data:
            changes = parse_unified_diff(sys.stdin.read())
        else:
            changes = parse_git_diff(list(diff_args) if diff_args else None)

        if not changes:
            console = Console(stderr=True)
            console.print("No changes found.", style="yellow")
            sys.exit(0)

        root = build_tree(changes)
        root = sort_tree(root, sort_by=sort_mode)
        config = RenderConfig(columns=columns, bar_width=bar_width, sort_by=sort_mode, max_depth=max_depth)
        diff_tree = DiffTree(root, config=config)
        console = Console()
        console.print(diff_tree)

    except Exception as e:
        console = Console(stderr=True)
        console.print(f"Error: {e}", style="bold red")
        sys.exit(1)


if __name__ == "__main__":
    main()
