"""Shared CLI option constants for props commands."""

from __future__ import annotations

import typer

from props.cli.types import DEFINITION_ID, SNAPSHOT_SLUG

# Arguments
ARG_SNAPSHOT = typer.Argument(..., help="Snapshot slug (under properties/specimens)", click_type=SNAPSHOT_SLUG)

# Options - General
OPT_MODEL = typer.Option("gpt-5", help="Model id")
OPT_VERBOSE = typer.Option(False, "--verbose", "-v", help="Enable verbose output")

# Options - Model Selection
OPT_OPTIMIZER_MODEL = typer.Option("gpt-5.1", help="Model for prompt optimizer/reflection agent")
OPT_CRITIC_MODEL = typer.Option("gpt-5.1-codex-mini", help="Model for critic execution")
OPT_GRADER_MODEL = typer.Option("gpt-5.1-codex-mini", help="Model for grader execution")

# Options - Output
OPT_OUT_DIR = typer.Option(None, "--out-dir", "-o", help="Output directory")
OPT_PROMPT_SHA256 = typer.Option(None, "--prompt-sha256", "-p", help="Prompt SHA256 to improve")

# Options - Snapshot & Files
OPT_FILES_FILTER = typer.Option(None, "--files", help="Limit review to specific files (relative paths)")

# Options - Parallelism
OPT_MAX_PARALLEL = typer.Option(4, "--max-parallel", help="Maximum number of parallel operations")

# Options - LLM Proxy (part of unified backend)
DEFAULT_LLM_PROXY_URL = "http://props-backend:8000"
LLM_PROXY_URL_ENVVAR = "PROPS_LLM_PROXY_URL"
OPT_LLM_PROXY_URL = typer.Option(
    DEFAULT_LLM_PROXY_URL, "--llm-proxy-url", envvar=LLM_PROXY_URL_ENVVAR, help="URL of the LLM proxy"
)

# Options - Timeout
DEFAULT_TIMEOUT_SECONDS = 3600
OPT_TIMEOUT_SECONDS = typer.Option(DEFAULT_TIMEOUT_SECONDS, "--timeout", help="Max seconds before container timeout")

# Options - Agent Definition
OPT_DEFINITION_ID = typer.Option(
    "critic",
    "--definition-id",
    "-d",
    help="Agent definition ID (e.g., 'critic', 'critic-v1')",
    click_type=DEFINITION_ID,
)
