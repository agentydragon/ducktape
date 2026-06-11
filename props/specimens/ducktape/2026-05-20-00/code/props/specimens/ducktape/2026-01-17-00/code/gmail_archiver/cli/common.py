"""Shared CLI types and utilities."""

from pathlib import Path
from typing import Annotated

import typer

from gmail_archiver.gmail_client import GmailClient

# Default token file location
GMAIL_TOKEN_FILE = Path.home() / ".gmail-mcp" / "token.json"


def get_client(token_file: Path | None) -> GmailClient:
    return GmailClient(token_file or GMAIL_TOKEN_FILE)


# Reusable option type annotations
TokenFileOption = Annotated[Path | None, typer.Option("--token-file", "-t", help="Path to Gmail OAuth token file")]

# Dry run with interactive default (None = prompt user)
DryRunOption = Annotated[
    bool | None,
    typer.Option(
        "--dry-run/--no-dry-run",
        help="Preview mode (--dry-run) or execute immediately (--no-dry-run). Default: interactive prompt.",
    ),
]

# Dry run with dry-run default (True = preview by default)
DryRunDefaultTrueOption = Annotated[
    bool,
    typer.Option(
        "--dry-run/--no-dry-run",
        help="Preview changes without modifying emails (default: dry-run, use --no-dry-run to actually apply)",
    ),
]
