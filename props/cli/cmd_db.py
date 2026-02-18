"""Database management commands: recreate, backup, restore, sync-specimen."""

from __future__ import annotations

import gzip
import subprocess
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from props.db.database import Database
from props.db.setup import ensure_database_exists
from props.db.sync.model_metadata import sync_model_metadata_with_session
from props.db.sync.sync import SpecimenBundle, sync_specimen

# Database subcommand group
db_app = typer.Typer(help="Database management commands")


# Typer Option defaults must not be created in function signatures (ruff B008)
SYNC_SPECIMEN_CODE_TAR_OPT = typer.Option(..., "--code-tar", help="Path to uncompressed code tar")
SYNC_SPECIMEN_DATA_YAML_OPT = typer.Option(
    ..., "--data-yaml", help="Path to merged data YAML (snapshot_slug + split + issues)"
)


def cmd_sync_specimen(
    ctx: typer.Context, code_tar: Path = SYNC_SPECIMEN_CODE_TAR_OPT, data_yaml: Path = SYNC_SPECIMEN_DATA_YAML_OPT
) -> None:
    """Sync a specimen from bundle artifacts (code tar + data YAML).

    The code tar must be an uncompressed tar file containing the code/ directory.
    The data YAML must have {snapshot_slug, split, issues} structure.
    The snapshot slug is read from the data YAML.

    Example:
        props db sync-specimen \\
            --code-tar bazel-bin/props/specimens/ducktape/2026-01-17-00/specimen_code.tar \\
            --data-yaml bazel-bin/props/specimens/ducktape/2026-01-17-00/specimen_data.yaml
    """
    db: Database = ctx.obj
    console = Console()

    # Create bundle (reads slug from data YAML)
    bundle = SpecimenBundle.from_paths(code_tar, data_yaml)

    console.print(f"Syncing {bundle.slug} from bundle artifacts...")
    with db.session() as session:
        sync_specimen(session, bundle)
        session.commit()

    console.print("[green]✓[/green] Sync completed successfully")


def cmd_db_recreate(
    ctx: typer.Context, yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt")
) -> None:
    """Recreate database from scratch (destructive - drops all tables/views/policies).

    This command will:
    1. Ensure database exists
    2. Drop all existing schema objects (tables, views, RLS policies, functions)
    3. Run Alembic migrations to recreate schema

    Note: This does NOT sync specimens. Use 'props db sync-specimen' to sync individual specimens.

    Requires database connection configured via environment variables (postgres superuser).
    """
    if not yes:
        typer.echo("⚠️  WARNING: This will DELETE ALL data in the database!")
        confirm = typer.prompt("Type 'yes' to confirm")
        if confirm != "yes":
            typer.echo("Aborted")
            raise typer.Exit(1)

    # Ensure databases exist before trying to connect
    typer.echo("Ensuring databases exist...")
    db: Database = ctx.obj
    ensure_database_exists(db.config, db.config.database, drop_existing=False)

    # Recreate schema and sync model metadata
    console = Console()
    console.print("Recreating database schema...")
    db.recreate()
    console.print("✓ Database schema recreated")

    console.print("Syncing model metadata...")
    with db.session() as session:
        sync_model_metadata_with_session(session)
        session.commit()
    console.print("✓ Model metadata synced")
    console.print("\nTo sync specimens, use: props db sync-specimen --code-tar <tar> --data-yaml <yaml>")


def get_default_backup_dir() -> Path:
    return Path(".devenv/state/pg_backups")


# Typer Option defaults must not be created in function signatures (ruff B008)
BACKUP_OUTPUT_OPT = typer.Option(
    None,
    "--output",
    "-o",
    help="Output file path. Defaults to .devenv/state/pg_backups/props_backup_<timestamp>.sql.gz",
)
BACKUP_PLAIN_OPT = typer.Option(False, "--plain", help="Output plain SQL instead of gzipped")


def cmd_db_backup(output: Path | None = BACKUP_OUTPUT_OPT, plain: bool = BACKUP_PLAIN_OPT) -> None:
    """Create a database backup. Uses PG* environment variables set by devenv."""
    console = Console()

    # Determine output path
    if output is None:
        backup_dir = get_default_backup_dir()
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = ".sql" if plain else ".sql.gz"
        output = backup_dir / f"props_backup_{timestamp}{suffix}"

    # pg_dump uses PG* env vars automatically (PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE)
    console.print(f"Creating backup: {output}")

    if plain:
        with output.open("w") as f:
            result = subprocess.run(["pg_dump"], stdout=f, stderr=subprocess.PIPE, check=False)
    else:
        with gzip.open(output, "wt") as f:
            result = subprocess.run(["pg_dump"], capture_output=True, check=False)
            if result.returncode == 0:
                f.write(result.stdout.decode())

    if result.returncode != 0:
        console.print(f"[red]Backup failed:[/red] {result.stderr.decode()}")
        raise typer.Exit(1)

    size_mb = output.stat().st_size / (1024 * 1024)
    console.print(f"[green]✓[/green] Backup complete: {output} ({size_mb:.1f} MB)")


RESTORE_BACKUP_FILE_ARG = typer.Argument(..., help="Backup file to restore from (.sql or .sql.gz)")
RESTORE_YES_OPT = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt")


def cmd_db_restore(backup_file: Path = RESTORE_BACKUP_FILE_ARG, yes: bool = RESTORE_YES_OPT) -> None:
    """Restore database from a backup file. Accepts .sql and .sql.gz. WARNING: Overwrites all data."""
    console = Console()

    if not backup_file.exists():
        console.print(f"[red]Error:[/red] Backup file not found: {backup_file}")
        raise typer.Exit(1)

    if not yes:
        console.print(f"[yellow]⚠️  WARNING:[/yellow] This will DELETE ALL data and restore from {backup_file}")
        confirm = typer.prompt("Type 'yes' to confirm")
        if confirm != "yes":
            console.print("Aborted")
            raise typer.Exit(1)

    # psql uses PG* env vars automatically
    cmd = ["psql", "-v", "ON_ERROR_STOP=1"]

    console.print(f"Restoring from: {backup_file}")

    # Determine if gzipped
    is_gzipped = backup_file.suffix == ".gz" or str(backup_file).endswith(".sql.gz")

    if is_gzipped:
        with gzip.open(backup_file, "rt") as f:
            sql_content = f.read()
        result = subprocess.run(cmd, input=sql_content, text=True, capture_output=True, check=False)
    else:
        with backup_file.open() as f:
            result = subprocess.run(cmd, stdin=f, capture_output=True, text=True, check=False)

    if result.returncode != 0:
        console.print(f"[red]Restore failed:[/red] {result.stderr}")
        raise typer.Exit(1)

    console.print("[green]✓[/green] Restore complete")


def cmd_db_list_backups() -> None:
    console = Console()
    backup_dir = get_default_backup_dir()

    if not backup_dir.exists():
        console.print(f"No backup directory found at {backup_dir}")
        return

    backups = sorted(backup_dir.glob("props_backup_*.sql*"), key=lambda p: p.stat().st_mtime, reverse=True)

    if not backups:
        console.print("No backups found")
        return

    table = Table(title="Available Backups")
    table.add_column("File", style="cyan")
    table.add_column("Size", justify="right")
    table.add_column("Created", style="dim")

    for backup in backups:
        stat = backup.stat()
        size_mb = stat.st_size / (1024 * 1024)
        created = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        table.add_row(backup.name, f"{size_mb:.1f} MB", created)

    console.print(table)


# Register commands
db_app.command("sync-specimen")(cmd_sync_specimen)
db_app.command("recreate")(cmd_db_recreate)
db_app.command("backup")(cmd_db_backup)
db_app.command("restore")(cmd_db_restore)
db_app.command("list-backups")(cmd_db_list_backups)
