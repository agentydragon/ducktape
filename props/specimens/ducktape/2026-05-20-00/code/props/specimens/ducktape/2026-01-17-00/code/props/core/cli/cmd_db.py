"""Database management commands: sync, recreate, backup, restore."""

from __future__ import annotations

import gzip
import subprocess
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from props.core.db.config import DatabaseConfig, get_database_config
from props.core.db.session import get_session, recreate_database
from props.core.db.setup import ensure_database_exists
from props.core.db.sync.sync import FullSyncResult, sync_all

# Database subcommand group
db_app = typer.Typer(help="Database management commands")


def ensure_databases_exist(config: DatabaseConfig) -> None:
    """Ensure eval_results database exists.

    Uses the unified helper from setup.py for database creation.
    Tests create per-test databases (props_test_*), not a shared test database.
    Note: agent_user role was deprecated - temporary users are now created per-agent instead.
    """
    ensure_database_exists(config, config.admin.database, drop_existing=False)


def recreate_database_and_sync(*, use_staged: bool = False) -> FullSyncResult:
    """Recreate database from scratch (destructive).

    Drops all tables/views/policies, creates fresh schema, and syncs all data
    (snapshots, issues, examples, model metadata, and agent definitions).

    Args:
        use_staged: If True, read agent definitions from staged files (index)
                    instead of HEAD. Skips the dirty check for development.

    Returns:
        Combined results from all sync operations
    """
    # Recreate schema (tables, RLS, roles)
    recreate_database()

    # Sync all data sources into fresh database
    with get_session() as session:
        return sync_all(session, use_staged=use_staged)


def print_sync_result(console: Console, result: FullSyncResult) -> None:
    """Print sync result summary table.

    Args:
        console: Rich console for output
        result: Sync result to display
    """
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Type", style="cyan")
    table.add_column("Stats")
    table.add_row("Snapshots", result.snapshot_stats.summary_text)
    table.add_row("Issues", result.issue_stats.summary_text)
    table.add_row("Snapshot files", result.snapshot_file_stats.summary_text)
    table.add_row("File sets", result.file_set_stats.summary_text)
    table.add_row("Model metadata", result.model_metadata_stats.summary_text)
    console.print(table)


def cmd_sync(
    use_staged: bool = typer.Option(
        False, "--use-staged", help="Read agent definitions from staged files instead of HEAD"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate without committing (rollback after sync)"),
) -> None:
    """Sync snapshots, issues, files, file sets, model metadata, and agent definitions from source to DB."""
    console = Console()
    with get_session() as session:
        result = sync_all(session, use_staged=use_staged, dry_run=dry_run)
    if dry_run:
        console.print("[yellow]DRY-RUN:[/yellow] Validation passed, no changes committed")
    print_sync_result(console, result)


def cmd_db_recreate(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    use_staged: bool = typer.Option(
        False, "--use-staged", help="Read agent definitions from staged files instead of HEAD"
    ),
) -> None:
    """Recreate database from scratch (destructive - drops all tables/views/policies).

    This command will:
    1. Ensure database exists (eval_results)
    2. Drop all existing schema objects (tables, views, RLS policies, functions)
    3. Run Alembic migrations to recreate schema
    4. Sync all data from filesystem (snapshots, issues, files, file sets, model metadata, agent definitions)

    Note: Temporary database users are created per-agent instead of a shared agent_user role.
          Schema creation (step 3) runs all Alembic migrations, which define tables, views, RLS, etc.

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
    db_config = get_database_config()
    ensure_databases_exist(db_config)

    # Connect and recreate (includes full sync)
    console = Console()
    console.print("Recreating database schema...")
    result = recreate_database_and_sync(use_staged=use_staged)
    console.print("✓ Database recreated:")

    print_sync_result(console, result)


def get_default_backup_dir() -> Path:
    """Get default backup directory (.devenv/state/pg_backups)."""
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
    """Create a database backup.

    By default, saves gzipped SQL to .devenv/state/pg_backups/.
    Use --output to specify a custom path, --plain to skip compression.

    Uses PG* environment variables set by devenv.
    """
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
    """Restore database from a backup file.

    Accepts both plain .sql and gzipped .sql.gz files.
    WARNING: This will overwrite all existing data!

    Uses PG* environment variables set by devenv.
    """
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
    """List available backups in the default backup directory."""
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
db_app.command("sync")(cmd_sync)
db_app.command("recreate")(cmd_db_recreate)
db_app.command("backup")(cmd_db_backup)
db_app.command("restore")(cmd_db_restore)
db_app.command("list-backups")(cmd_db_list_backups)
