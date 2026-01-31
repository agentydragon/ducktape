"""Workflow configuration models for CI decision logic.

These models define our internal workflows.yaml format - trigger rules,
inputs, and secrets for each reusable workflow.

For GitHub Actions workflow schema (Step, Job, Workflow), see gha.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Discriminator, Field, Tag


class AlwaysTrigger(BaseModel):
    """Workflow that always runs."""

    kind: Literal["always"] = "always"


class BazelQueryTrigger(BaseModel):
    """Workflow triggered by a Bazel query over affected targets.

    The query field is a Bazel query expression where ``$targets`` is replaced
    with ``set(...)`` of the affected targets.  The workflow triggers when the
    query returns at least one result.

    Example: ``kind(".*_test rule", $targets)`` triggers only when the affected
    set contains test targets.
    """

    kind: Literal["bazel_query"] = "bazel_query"
    query: str


class PathPatternTrigger(BaseModel):
    """Workflow triggered by file path pattern."""

    kind: Literal["path"] = "path"
    pattern: str


WorkflowTrigger = Annotated[
    Annotated[AlwaysTrigger, Tag("always")]
    | Annotated[BazelQueryTrigger, Tag("bazel_query")]
    | Annotated[PathPatternTrigger, Tag("path")],
    Discriminator("kind"),
]


class WorkflowConfig(BaseModel):
    """Configuration for a workflow from workflows.yaml."""

    trigger: WorkflowTrigger
    targets: bool = False
    inputs: dict[str, str] = Field(default_factory=dict)
    secrets: list[str] = Field(default_factory=list)


def _parse_bazel_package(label: str) -> str:
    """Extract the package path from a Bazel label.

    Examples:
        "//:wheel" -> ""
        "//headscale_cleanup:wheel" -> "headscale_cleanup"
    """
    return label.removeprefix("//").split(":")[0]


class ReleaseConfig(BaseModel):
    """Configuration for a package release workflow.

    wheel_path is derived from bazel_target's package path.
    wheel_name and latest_release_tag are computed from the manifest key
    in generate_release_config.
    """

    bazel_target: str
    release_body: str
    apt_packages: list[str] = Field(default_factory=list)

    @property
    def wheel_path(self) -> str:
        package = _parse_bazel_package(self.bazel_target)
        return f"bazel-bin/{package}" if package else "bazel-bin"


class WorkflowManifest(BaseModel):
    """Collection of all workflow configurations."""

    workflows: dict[str, WorkflowConfig]
    releases: dict[str, ReleaseConfig] = Field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: Path) -> WorkflowManifest:
        """Load from YAML file."""
        with path.open() as f:
            data = yaml.safe_load(f)
        return cls.model_validate(data)
