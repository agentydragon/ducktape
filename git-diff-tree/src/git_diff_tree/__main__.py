"""CLI entry point for git-diff-tree."""

import sys
from typing import Optional

import click
from rich.console import Console

from .config import Column, RenderConfig
from .parser import parse_git_diff
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
    default=None,
    help="Columns to display (comma-separated): tree,counts,bars,percentages",
)
@click.option(
    "--no-counts",
    is_flag=True,
    help="DEPRECATED: Use --columns instead. Hide +/- count columns",
)
@click.option(
    "--no-bars",
    is_flag=True,
    help="DEPRECATED: Use --columns instead. Hide progress bar columns",
)
@click.option(
    "--no-percentages",
    is_flag=True,
    help="DEPRECATED: Use --columns instead. Hide percentage column",
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
    columns: Optional[str],
    no_counts: bool,
    no_bars: bool,
    no_percentages: bool,
    bar_width: int,
    max_depth: Optional[int],
) -> None:
    """
    Visualize git diff as a tree with progress bars.

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
    # Sort alphabetically without progress bars
    git-diff-tree --sort alpha --no-bars
    """
    try:
        # Parse git diff
        changes = parse_git_diff(list(diff_args) if diff_args else None)

        if not changes:
            console = Console(stderr=True)
            console.print("No changes found.", style="yellow")
            sys.exit(0)

        # Build tree structure
        root = build_tree(changes)

        # Sort tree
        sort_tree(root, sort_by=sort)

        # Build render configuration
        if columns is not None:
            # Use new --columns flag
            column_list = []
            for col_name in columns.split(","):
                col_name = col_name.strip().upper()
                try:
                    column_list.append(Column[col_name])
                except KeyError:
                    console = Console(stderr=True)
                    console.print(
                        f"Error: Unknown column '{col_name}'. "
                        f"Valid options: {', '.join(c.value for c in Column)}",
                        style="bold red",
                    )
                    sys.exit(1)
        else:
            # Fall back to deprecated boolean flags
            column_list = [Column.TREE]  # Always include tree
            if not no_counts:
                column_list.append(Column.COUNTS)
            if not no_bars:
                column_list.append(Column.BARS)
            if not no_percentages:
                column_list.append(Column.PERCENTAGES)

        config = RenderConfig(
            columns=column_list,
            bar_width=bar_width,
            sort_by=sort,
            max_depth=max_depth,
        )

        # Render tree
        renderer = DiffTreeRenderer(config=config)
        renderer.render(root, max_depth=max_depth)

    except Exception as e:
        console = Console(stderr=True)
        console.print(f"Error: {e}", style="bold red")
        sys.exit(1)


if __name__ == "__main__":
    main()
