"""Shared CLI utilities for props commands."""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from pathlib import Path

import tiktoken
import typer

from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleSpec, WholeSnapshotExample
from props.core.runs_context import format_timestamp_session


def save_prompt_to_tmp(stem: str, text: str) -> Path:
    """Save prompt text to temp dir and print summary. Prints approximate token count."""
    tmpdir = Path(tempfile.gettempdir()) / "adgn_codex_prompts"
    tmpdir.mkdir(parents=True, exist_ok=True)
    ts = format_timestamp_session()
    outfile = tmpdir / f"{stem}_{ts}.md"
    outfile.write_text(text, encoding="utf-8")
    tokens = len(tiktoken.get_encoding("cl100k_base").encode(text))
    print(f"Saved prompt: {outfile} (approx tokens: {tokens})")
    return outfile


def make_example_from_files(
    snapshot_slug: SnapshotSlug, all_files: Mapping[Path, object], requested_files: list[str] | None
) -> ExampleSpec:
    """Create an ExampleSpec from file filter. Only supports whole-snapshot for now."""
    # No filter → return WholeSnapshotExample
    if requested_files is None:
        return WholeSnapshotExample(snapshot_slug=snapshot_slug)

    # Per-file filtering requires database lookup to get/create trigger_set_id
    # This would require session access and trigger set creation
    # For now, CLI only supports whole-snapshot review
    typer.echo("Error: Per-file filtering is not yet supported in CLI", err=True)
    typer.echo("Use --files without arguments to review entire snapshot", err=True)
    raise typer.Exit(1)
