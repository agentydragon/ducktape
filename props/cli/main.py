"""Typer-based CLI entry for props.

Incremental migration target: we will gradually move subcommands here.
"""

from __future__ import annotations

import logging
from typing import Annotated

import typer
from rich.traceback import install as rich_traceback_install
from typer_di import TyperDI

from props.cli.cmd_db import db_app
from props.db.database import Database
from util.logging import LogLevel, make_logging_callback

logger = logging.getLogger(__name__)

app = TyperDI(help="props — properties tooling", add_completion=False)

# Subcommand groups
app.add_typer(db_app, name="db")

# Configure logging via shared callback (default: WARNING level for props)
# Then add database initialization on top
_logging_callback = make_logging_callback(default_level=LogLevel.WARNING)


@app.callback()
def _init_logging_and_db(
    ctx: typer.Context,
    log_output: Annotated[
        str,
        typer.Option(
            "--log-output",
            envvar="ADGN_LOG_OUTPUT",
            help="Where to send logs: 'stderr', 'stdout', 'none', or a file path",
        ),
    ] = "stderr",
    log_level: Annotated[str, typer.Option("--log-level", envvar="ADGN_LOG_LEVEL", help="Log level")] = "WARNING",
) -> None:
    """Global callback to configure logging and initialize database for all subcommands."""
    # First, configure logging via the shared callback
    _logging_callback(log_output=log_output, log_level=log_level)

    # Suppress verbose OpenAI HTTP request/response logging (too noisy at DEBUG level)
    logging.getLogger("openai.http").setLevel(logging.WARNING)
    logging.getLogger("openai._base_client").setLevel(logging.WARNING)

    # Configure Rich traceback for CLI errors (increased detail for debugging)
    rich_traceback_install(show_locals=True, max_frames=50, extra_lines=2, width=120)

    # Create Database once at CLI entry. Stored on typer context for explicit DI.
    db = Database.from_env()
    ctx.obj = db


# GEPA command (optional - requires gepa package)
try:
    from props.cli.cmd_gepa import cmd_gepa

    app.command("gepa")(cmd_gepa)
except ImportError:
    pass


if __name__ == "__main__":
    app()
