"""CLI to extract open #issue nodes from a Tana export into TanaPaste."""

import sys
from pathlib import Path

from tana.export.convert import RenderContext
from tana.query.filters import filter_open_issues
from tana.workspace import Workspace

EXPECTED_ARGS = 2


def main():
    if len(sys.argv) != EXPECTED_ARGS:
        print("Usage: python tana_issues_to_tanapaste.py <input.json>", file=sys.stderr)
        print(
            "\nExtracts all #issue nodes (where Status != Done/Cancelled) and outputs TanaPaste to stdout",
            file=sys.stderr,
        )
        sys.exit(1)

    input_path = Path(sys.argv[1])

    if not input_path.exists():
        print(f"Error: Input file '{input_path}' not found.", file=sys.stderr)
        sys.exit(1)

    # Load and process
    workspace = Workspace.load(input_path)
    store = workspace.graph
    issue_ids = list(filter_open_issues(store))

    # Export the given issues as a flat TanaPaste document
    lines = ["%%tana%%"]
    ctx = RenderContext(store, "tana")
    for issue_id in issue_ids:
        lines.extend(ctx.render_node(store[issue_id]))
        lines.append("")  # Empty line between issues

    # Output to stdout
    print("\n".join(lines).rstrip())

    # Report to stderr
    print(f"Open issues exported: {len(issue_ids)}", file=sys.stderr)


if __name__ == "__main__":
    main()
