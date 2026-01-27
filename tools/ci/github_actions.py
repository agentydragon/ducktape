"""GitHub Actions workflow models and output utilities.

Pydantic models representing the GitHub Actions workflow YAML schema.
These are used to generate and validate workflow files.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from tools.env_utils import get_required_env, get_required_env_path

_logger = logging.getLogger(__name__)


# === GitHub Actions Environment ===


class PushStrategy(StrEnum):
    """How to determine affected targets on push events."""

    INCREMENTAL = "incremental"  # Compare HEAD vs HEAD~1
    FULL = "full"  # Build/test all targets (//...)


class CIEnvironment(BaseModel):
    """GitHub Actions CI environment variables.

    Captures GHA-provided env vars at startup and threads through as DI.
    """

    workspace: Path
    output_path: Path
    event_name: str
    base_ref: str
    push_strategy: PushStrategy

    @classmethod
    def from_env(cls) -> CIEnvironment:
        """Load CI environment from os.environ. Raises on missing required vars."""
        return cls(
            workspace=Path(os.environ.get("GITHUB_WORKSPACE") or Path.cwd()),
            output_path=get_required_env_path("GITHUB_OUTPUT"),
            event_name=get_required_env("GITHUB_EVENT_NAME"),
            base_ref=os.environ.get("GITHUB_BASE_REF", ""),
            push_strategy=PushStrategy(get_required_env("CI_PUSH_STRATEGY")),
        )

    @property
    def is_pull_request(self) -> bool:
        return self.event_name == "pull_request"

    def write_outputs(self, outputs: Mapping[str, str | bool]) -> None:
        """Write outputs to GitHub Actions output file and log them.

        Bool values are formatted as "true"/"false".
        """
        formatted = {k: ("true" if v else "false") if isinstance(v, bool) else v for k, v in outputs.items()}
        self.output_path.write_text("".join(f"{k}={v}\n" for k, v in formatted.items()))
        for key, value in formatted.items():
            _logger.info("%s=%s", key, value)


# === Workflow Schema Models ===


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
