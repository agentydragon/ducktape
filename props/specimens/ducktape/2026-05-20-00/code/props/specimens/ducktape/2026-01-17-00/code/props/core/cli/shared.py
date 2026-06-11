"""Shared CLI utilities for props commands."""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import tiktoken
import typer

from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleSpec, WholeSnapshotExample
from props.core.runs_context import format_timestamp_session


@dataclass(frozen=True)
class BuildOptions:
    sandbox: str
    skip_git_repo_check: bool
    full_auto: bool
    extra_configs: list[str] | None = None


def save_prompt_to_tmp(stem: str, text: str) -> Path:
    """Save prompt text under the system temp dir and print a short summary.

    File name: <stem>_<ts>.md. Prints an approximate token count using tiktoken.
    """
    tmpdir = Path(tempfile.gettempdir()) / "adgn_codex_prompts"
    tmpdir.mkdir(parents=True, exist_ok=True)
    ts = format_timestamp_session()
    outfile = tmpdir / f"{stem}_{ts}.md"
    outfile.write_text(text, encoding="utf-8")
    tokens = len(tiktoken.get_encoding("cl100k_base").encode(text))
    print(f"Saved prompt: {outfile} (approx tokens: {tokens})")
    return outfile


def build_cmd(model: str, workdir: Path, opts: BuildOptions) -> list[str]:
    cmd: list[str] = ["codex", "exec", "--model", model, "--sandbox", opts.sandbox, "-C", str(workdir)]
    if opts.extra_configs:
        for c in opts.extra_configs:
            cmd.extend(["-c", c])
    if opts.full_auto:
        cmd.append("--full-auto")
    if opts.skip_git_repo_check:
        cmd.append("--skip-git-repo-check")
    return cmd


def make_example_from_files(
    snapshot_slug: SnapshotSlug, all_files: Mapping[Path, object], requested_files: list[str] | None
) -> ExampleSpec:
    """Create an ExampleSpec from file filter, with validation.

    Args:
        snapshot_slug: Snapshot identifier
        all_files: All available files from snapshot
        requested_files: Optional list of relative paths to filter to

    Returns:
        WholeSnapshotExample if no filter requested.
        Currently only supports whole-snapshot; per-file requires trigger_set_id from database.

    Raises:
        typer.Exit: If requested files are invalid or not found
        NotImplementedError: If requested_files is not None (per-file not yet supported in CLI)
    """
    # No filter → return WholeSnapshotExample
    if requested_files is None:
        return WholeSnapshotExample(snapshot_slug=snapshot_slug)

    # Per-file filtering requires database lookup to get/create trigger_set_id
    # This would require session access and trigger set creation
    # For now, CLI only supports whole-snapshot review
    typer.echo("Error: Per-file filtering is not yet supported in CLI", err=True)
    typer.echo("Use --files without arguments to review entire snapshot", err=True)
    raise typer.Exit(1)
