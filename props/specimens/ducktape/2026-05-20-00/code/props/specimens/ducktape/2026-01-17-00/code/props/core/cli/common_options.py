"""Shared CLI option constants for props commands."""

from __future__ import annotations

import typer

from props.core.cli.types import DEFINITION_ID, SNAPSHOT_SLUG
from props.core.docker_env import WORKING_DIR as CRITIC_WORKDIR

# Arguments
ARG_WORKDIR = typer.Argument(..., exists=True, file_okay=False, resolve_path=True)
ARG_SCOPE = typer.Argument(..., help="Freeform scope description (e.g. 'all files under src/**')")
ARG_SNAPSHOT = typer.Argument(..., help="Snapshot slug (under properties/specimens)", click_type=SNAPSHOT_SLUG)
ARG_ISSUE_ID = typer.Argument(..., help="Issue id to lint (must have should_flag=true)")
ARG_OCCURRENCE = typer.Argument(..., help="0-based occurrence index")
ARG_CMD_LIST = typer.Argument(..., help="Command to run inside container")
ARG_PROMPT = typer.Argument(..., help="Candidate critic system prompt to evaluate across snapshots")
ARG_CRITIC_RUN_ID = typer.Argument(..., help="Critic run ID (UUID) from database")

# Options - General
OPT_MODEL = typer.Option("gpt-5", help="Model id")
OPT_DRY_RUN = typer.Option(False, help="Compose prompt only; do not run")

# Options - Model Selection (shared between prompt-optimize and gepa)
OPT_OPTIMIZER_MODEL = typer.Option("gpt-5.1", help="Model for prompt optimizer/reflection agent")
OPT_CRITIC_MODEL = typer.Option("gpt-5.1-codex-mini", help="Model for critic execution")
OPT_GRADER_MODEL = typer.Option("gpt-5.1-codex-mini", help="Model for grader execution")
OPT_FINAL_ONLY = typer.Option(False, help="Print only final message")
OPT_OUTPUT_FINAL_MESSAGE = typer.Option(None, help="Write final message to this path")
OPT_ALLOW_GENERAL = typer.Option(False, help="Allow general code-quality findings beyond formal properties")
OPT_OUTPUT_DIR = typer.Option(None, help="Root directory for run artifacts")
OPT_OUT_DIR = typer.Option(None, "--out-dir", "-o", help="Output directory (default: temp dir in /tmp)")
OPT_PROMPT_SHA256 = typer.Option(
    None, "--prompt-sha256", "-p", help="Prompt SHA256 to improve (default: best recent prompt)"
)
OPT_MAX_ITERS = typer.Option(10, help="Maximum number of prompt evaluations (tool calls)")
OPT_VERBOSE = typer.Option(False, "--verbose", "-v", help="Enable verbose output")

# Options - Display
DEFAULT_MAX_LINES = 10  # Default max lines per event in verbose display
OPT_MAX_LINES = typer.Option(DEFAULT_MAX_LINES, "--max-lines", "-m", help="Max lines per event in verbose display")

# Options - Context & Environment
OPT_CONTEXT = typer.Option(
    "minimal", help=("Agent context: minimal (no extra servers) or props (mount /props via docker MCP)")
)
OPT_WORKDIR_CRITIC = typer.Option(CRITIC_WORKDIR, "--workdir", help="Container working dir (default: /workspace)")

# Options - Snapshot & Files
OPT_SNAPSHOT = typer.Option(None, "--snapshot", help="Snapshot slug", click_type=SNAPSHOT_SLUG)
OPT_SNAPSHOT_REQUIRED = typer.Option(..., "--snapshot", help="Snapshot slug (required)", click_type=SNAPSHOT_SLUG)
OPT_FILES_FILTER = typer.Option(None, "--files", help="Limit review to specific files (relative paths)")

# Options - Grading & Critique
OPT_CRITIQUE = typer.Option(..., "--critique", exists=True, help="Path to the input critique JSON file")
OPT_MAX_PARALLEL = typer.Option(4, "--max-parallel", help="Maximum number of parallel operations")

# Options - Docker Execution
OPT_INTERACTIVE = typer.Option(False, "-i", help="Attach STDIN (docker exec -i)")
OPT_TTY_EXEC = typer.Option(False, "-t", help="Allocate TTY (docker exec -t)")
OPT_SKIP_GIT_REPO_CHECK = typer.Option(False, help="Pass --skip-git-repo-check to codex exec")
OPT_FULL_AUTO = typer.Option(False, help="Pass --full-auto to codex exec")

# Options - Runbook/Path Selection
OPT_RUNBOOK_PATH = typer.Option(
    None,
    "--path",
    exists=True,
    file_okay=False,
    resolve_path=True,
    help="Local code path to mount as /workspace (read-only)",
)
OPT_RUNBOOK_SNAPSHOT = typer.Option(
    None, "--snapshot", help="Snapshot slug to hydrate and mount as /workspace (read-only)", click_type=SNAPSHOT_SLUG
)

# Options - Agent Definition
OPT_DEFINITION_ID = typer.Option(
    "critic",
    "--definition-id",
    "-d",
    help="Agent definition ID (e.g., 'critic', 'critic-v1'). Default: 'critic'.",
    click_type=DEFINITION_ID,
)
