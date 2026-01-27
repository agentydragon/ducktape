"""Shared models for CI scripts.

Contains Pydantic models used by both ci_decide.py and generate_ci.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, Discriminator, Field, Tag

# GitHub Actions workflow models


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


# Workflow configuration models


class AlwaysTrigger(BaseModel):
    """Workflow that always runs."""

    kind: Literal["always"] = "always"


class BazelPatternTrigger(BaseModel):
    """Workflow triggered by Bazel target pattern."""

    kind: Literal["bazel"] = "bazel"
    pattern: str


class PathPatternTrigger(BaseModel):
    """Workflow triggered by file path pattern."""

    kind: Literal["path"] = "path"
    pattern: str


WorkflowTrigger = Annotated[
    Annotated[AlwaysTrigger, Tag("always")]
    | Annotated[BazelPatternTrigger, Tag("bazel")]
    | Annotated[PathPatternTrigger, Tag("path")],
    Discriminator("kind"),
]


class WorkflowConfig(BaseModel):
    """Configuration for a workflow from workflows.yaml."""

    trigger: WorkflowTrigger
    targets: bool = False
    inputs: dict[str, str] = Field(default_factory=dict)
    secrets: list[str] = Field(default_factory=list)


class WorkflowManifest(BaseModel):
    """Collection of all workflow configurations."""

    workflows: dict[str, WorkflowConfig]

    @classmethod
    def from_yaml(cls, path: Path) -> WorkflowManifest:
        """Load from YAML file."""
        with path.open() as f:
            data = yaml.safe_load(f)
        return cls.model_validate(data)
