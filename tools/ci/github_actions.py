"""GitHub Actions workflow models and output utilities.

Pydantic models representing the GitHub Actions workflow YAML schema.
These are used to generate and validate workflow files.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_logger = logging.getLogger(__name__)


def format_output(outputs: dict[str, str]) -> str:
    """Format outputs dict as GitHub Actions output file content."""
    return "".join(f"{k}={v}\n" for k, v in outputs.items())


def bool_output(value: bool) -> str:
    """Format bool as GitHub Actions output string."""
    return "true" if value else "false"


def write_outputs(outputs: dict[str, str]) -> None:
    """Write outputs to GITHUB_OUTPUT file and log them.

    Raises RuntimeError if GITHUB_OUTPUT is not set (expected in CI).
    """
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        raise RuntimeError("GITHUB_OUTPUT not set")
    Path(output_file).write_text(format_output(outputs))
    for key, value in outputs.items():
        _logger.info("%s=%s", key, value)


class Step(BaseModel):
    """A step in a GitHub Actions job."""

    name: str | None = None
    id: str | None = None
    uses: str | None = None
    run: str | None = None
    if_cond: str | None = Field(None, alias="if", serialization_alias="if")
    with_args: dict[str, Any] | None = Field(None, alias="with", serialization_alias="with")

    model_config = {"populate_by_name": True}


class Job(BaseModel):
    """A job in a GitHub Actions workflow."""

    name: str | None = None
    runs_on: str | None = Field(None, alias="runs-on", serialization_alias="runs-on")
    timeout_minutes: int | None = Field(None, alias="timeout-minutes", serialization_alias="timeout-minutes")
    needs: str | None = None
    if_cond: str | None = Field(None, alias="if", serialization_alias="if")
    uses: str | None = None
    with_args: dict[str, str] | None = Field(None, alias="with", serialization_alias="with")
    secrets: str | None = None
    outputs: dict[str, str] | None = None
    steps: list[Step] | None = None

    model_config = {"populate_by_name": True}


class Workflow(BaseModel):
    """A GitHub Actions workflow file."""

    name: str
    on: dict[str, Any] = Field(serialization_alias="on")
    concurrency: dict[str, Any]
    permissions: dict[str, str]
    jobs: dict[str, Job]

    model_config = {"populate_by_name": True}
