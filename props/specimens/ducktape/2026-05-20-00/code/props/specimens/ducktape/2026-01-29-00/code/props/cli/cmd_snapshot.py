"""Snapshot management commands: list, dump, fetch."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from typer_di import TyperDI

from cli_util.decorators import async_run
from props.cli import common_options as opt
from props.core.ids import SnapshotSlug
from props.db.models import Snapshot
from props.db.session import get_session
from props.db.snapshot_io import fetch_snapshot_to_path
from props.db.sync.export import _format_files

# Snapshot subcommand group
snapshot_app = TyperDI(help="Snapshot commands")


@async_run
async def cmd_snapshot_list() -> None:
    with get_session() as session:
        snapshots = session.query(Snapshot).all()
        slugs = sorted([s.slug for s in snapshots])

    for slug in slugs:
        typer.echo(str(slug))


@async_run
async def snapshot_dump(
    snapshot: SnapshotSlug = opt.ARG_SNAPSHOT,
    pretty: bool = typer.Option(True, help="Pretty-print JSON with indentation"),
) -> None:
    """Dump a snapshot's full structure as JSON."""
    try:
        # Load snapshot and issues from database (no source hydration needed for dump)
        with get_session() as session:
            db_snapshot = session.query(Snapshot).filter_by(slug=snapshot).one()

            # Build output structure directly from ORM
            output = {
                "slug": str(db_snapshot.slug),
                "issues": {
                    tp.tp_id: {
                        "rationale": tp.rationale,
                        "instances": [
                            {
                                "occurrence_id": occ.occurrence_id,
                                "files": _format_files(occ.ranges),
                                "note": occ.note,
                                "critic_scopes_expected_to_recall": [
                                    sorted(str(m.file_path) for m in scope.file_set.members)
                                    for scope in occ.critic_scopes_expected_to_recall
                                    if scope.file_set
                                ],
                            }
                            for occ in tp.occurrences
                        ],
                    }
                    for tp in db_snapshot.true_positives
                },
                "false_positives": {
                    fp.fp_id: {
                        "rationale": fp.rationale,
                        "instances": [
                            {
                                "occurrence_id": occ.occurrence_id,
                                "files": _format_files(occ.ranges),
                                "note": occ.note,
                                "relevant_files": sorted(str(rf.file_path) for rf in occ.relevant_file_orms),
                            }
                            for occ in fp.occurrences
                        ],
                    }
                    for fp in db_snapshot.false_positives
                },
            }

            indent = 2 if pretty else None
            print(json.dumps(output, indent=indent))
    except Exception as e:
        typer.echo(f"ERROR: Failed to load snapshot '{snapshot}': {e}")
        raise typer.Exit(2) from e


def snapshot_fetch(
    slug: Annotated[str, typer.Argument(help="Snapshot slug (e.g., 'ducktape/2025-11-26-00')")],
    output: Annotated[Path, typer.Argument(help="Output directory to extract snapshot into")],
) -> None:
    """Fetch snapshot from database and extract to filesystem."""
    try:
        fetch_snapshot_to_path(slug, output)
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Extracted: {output}")


# Register commands
snapshot_app.command("list")(cmd_snapshot_list)
snapshot_app.command("dump")(snapshot_dump)
snapshot_app.command("fetch")(snapshot_fetch)
