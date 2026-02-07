"""Shared CLI option constants for props commands."""

from __future__ import annotations

import typer

from props.cli.types import SNAPSHOT_SLUG

# Arguments
ARG_SNAPSHOT = typer.Argument(..., help="Snapshot slug (under properties/specimens)", click_type=SNAPSHOT_SLUG)

# Options - Model Selection
OPT_OPTIMIZER_MODEL = typer.Option("gpt-5.1", help="Model for critic developer agent")
OPT_CRITIC_MODEL = typer.Option("gpt-5.1-codex-mini", help="Model for critic execution")
OPT_GRADER_MODEL = typer.Option("gpt-5.1-codex-mini", help="Model for grader execution")

# Options - Output
OPT_OUT_DIR = typer.Option(None, "--out-dir", "-o", help="Output directory")

# Options - Timeout
OPT_TIMEOUT_SECONDS = typer.Option(3600, "--timeout", help="Max seconds before container timeout")
