"""Ground truth management commands: export."""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console

from props.core.ids import SnapshotSlug
from props.db.session import get_session
from props.db.sync.export import export_snapshot_issues

# Ground truth subcommand group
gt_app = typer.Typer(help="Ground truth management commands")


def _get_specimens_root() -> Path:
    specimens_root = os.environ.get("ADGN_PROPS_SPECIMENS_ROOT")
    if not specimens_root:
        raise typer.BadParameter(
            "ADGN_PROPS_SPECIMENS_ROOT environment variable not set. Set it to the path of your specimens repository."
        )
    return Path(specimens_root)


EXPORT_SNAPSHOT_ARG = typer.Argument(..., help="Snapshot slug to export (e.g., 'ducktape/2025-09-03-00')")
EXPORT_OUTPUT_OPT = typer.Option(
    None, "--output", "-o", help="Output directory. Defaults to $ADGN_PROPS_SPECIMENS_ROOT/<snapshot_slug>/"
)


def cmd_gt_export(snapshot_slug: str = EXPORT_SNAPSHOT_ARG, output: Path | None = EXPORT_OUTPUT_OPT) -> None:
    """Export ground truth (TPs/FPs) for a snapshot to YAML files."""
    console = Console()
    slug = SnapshotSlug(snapshot_slug)

    # Determine output directory
    if output is None:
        specimens_root = _get_specimens_root()
        output = specimens_root / snapshot_slug

    console.print(f"Exporting ground truth for [cyan]{slug}[/cyan] to [cyan]{output}[/cyan]")

    with get_session() as session:
        result = export_snapshot_issues(session, slug, output)

    console.print(
        f"[green]✓[/green] Exported {result.tp_count} TPs, {result.fp_count} FPs to {result.output_dir / 'issues'}"
    )


# Register commands
gt_app.command("export")(cmd_gt_export)
