"""CLI entry point for git-diff-tree."""

import sys
from typing import Optional

import click
from rich.console import Console

from .config import RenderConfig, parse_columns
from .parser import parse_diff_from_stdin, parse_git_diff
from .renderer import DiffTreeRenderer
from .tree import build_tree, sort_tree


@click.command()
@click.argument("diff_args", nargs=-1)
@click.option(
    "--sort",
    type=click.Choice(["size", "alpha"]),
    default="size",
    help="Sort mode: 'size' (default) or 'alpha'",
)
@click.option(
    "--columns",
    type=str,
    default="tree,counts,bars,percentages",
    help="Columns to display (comma-separated): tree,counts,bars,percentages",
)
@click.option(
    "--bar-width",
    type=int,
    default=20,
    help="Width of each progress bar (default: 20)",
)
@click.option(
    "--max-depth",
    type=int,
    default=None,
    help="Maximum tree depth to display",
)
def main(
    diff_args: tuple[str, ...],
    sort: str,
    columns: str,
    bar_width: int,
    max_depth: Optional[int],
) -> None:
    """
    Visualize git diff as a tree with progress bars.

    Can read from stdin (piped input) or run git diff directly.

    Examples:

    \b
    # Show unstaged changes
    git-diff-tree

    \b
    # Show changes between commits
    git-diff-tree HEAD~1 HEAD

    \b
    # Show staged changes
    git-diff-tree --cached

    \b
    # Use as a pager (read from stdin)
    git diff | git-diff-tree
    svn diff | git-diff-tree

    \b
    # Sort alphabetically without progress bars
    git-diff-tree --sort alpha --columns tree,counts
    """
    try:
        # Determine input source
        if not sys.stdin.isatty():
            # Reading from stdin (piped input)
            changes = parse_diff_from_stdin()
        else:
            # Interactive mode - run git diff
            changes = parse_git_diff(list(diff_args) if diff_args else None)

        if not changes:
            console = Console(stderr=True)
            console.print("No changes found.", style="yellow")
            sys.exit(0)

        # Build tree structure
        root = build_tree(changes)

        # Sort tree
        sort_tree(root, sort_by=sort)

        # Parse column configuration
        try:
            column_list = parse_columns(columns)
        except ValueError as e:
            console = Console(stderr=True)
            console.print(f"Error: {e}", style="bold red")
            sys.exit(1)

        config = RenderConfig(
            columns=column_list,
            bar_width=bar_width,
            sort_by=sort,
            max_depth=max_depth,
        )

        # Render tree
        renderer = DiffTreeRenderer(config=config)
        renderer.render(root)

    except Exception as e:
        console = Console(stderr=True)
        console.print(f"Error: {e}", style="bold red")
        sys.exit(1)


if __name__ == "__main__":
    main()
