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


def _parse_bazel_label(label: str) -> tuple[str, str]:
    """Parse a Bazel label into (package, target) parts.

    Examples:
        "//:ducktape_wheel" -> ("", "ducktape_wheel")
        "//headscale_cleanup:headscale_cleanup_wheel" -> ("headscale_cleanup", "headscale_cleanup_wheel")
    """
    label = label.removeprefix("//")
    if ":" in label:
        package, target = label.split(":", 1)
    else:
        package = label
        target = label.rsplit("/", 1)[-1]
    return package, target


class ReleaseConfig(BaseModel):
    """Configuration for a package release workflow.

    wheel_name and wheel_path are derived from bazel_target:
    - wheel_name = target name with _wheel suffix stripped
    - wheel_path = bazel-bin/<package> (or just bazel-bin for root targets)
    """

    bazel_target: str
    release_body: str
    apt_packages: list[str] = Field(default_factory=list)
    latest_release_tag: str | None = None

    @property
    def wheel_name(self) -> str:
        _, target = _parse_bazel_label(self.bazel_target)
        return target.removesuffix("_wheel")

    @property
    def wheel_path(self) -> str:
        package, _ = _parse_bazel_label(self.bazel_target)
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
