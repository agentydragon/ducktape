"""Workflow configuration models for CI decision logic.

These models define our internal workflows.yaml format - trigger rules,
inputs, and secrets for each reusable workflow.

For GitHub Actions workflow schema (Step, Job, Workflow), see gha.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, Discriminator, Field, Tag

from util.bazel.query import BazelLabel


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
    secrets: Literal["inherit"] | None = None
    rbe: bool = True
    events: frozenset[str] = frozenset({"push", "pull_request", "workflow_dispatch"})


class ExtraJobStep(BaseModel):
    """A step in an extra job, matching GitHub Actions step schema."""

    name: str | None = None
    id: str | None = None
    uses: str | None = None
    run: str | None = None
    if_cond: str | None = Field(None, alias="if")
    with_args: dict[str, str] | None = Field(None, alias="with")

    model_config = {"populate_by_name": True}


class ExtraJobConfig(BaseModel):
    """An extra job to splice into the release workflow."""

    needs: list[str] = Field(default_factory=list)
    runs_on: str = Field("ubuntu-latest", alias="runs-on")
    timeout_minutes: int = Field(30, alias="timeout-minutes")
    steps: list[ExtraJobStep] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class ReleaseTarget(BaseModel):
    """A build target in a release, with optional Nix flake input for downstream updates."""

    bazel_target: Annotated[BazelLabel, BeforeValidator(BazelLabel.parse)]
    flake_input: str | None = None


class ReleaseConfig(BaseModel):
    """Configuration for a package release in the consolidated release workflow.

    wheel_path is derived from the primary target's package path.
    wheel_name and latest_release_tag are computed from the manifest key.
    """

    targets: list[ReleaseTarget]
    release_body: str
    artifact_type: Literal["wheel", "binary"] = "wheel"
    test_targets: str | None = None
    update_claude_settings: bool = False
    apt_packages: list[str] = Field(default_factory=list)
    extra_jobs: dict[str, ExtraJobConfig] = Field(default_factory=dict)
    release_needs: list[str] = Field(default_factory=list)
    wheel_name: str | None = None

    @property
    def wheel_path(self) -> str:
        label = self.targets[0].bazel_target
        return f"bazel-bin/{label.package}" if label.package.parts else "bazel-bin"


class HarborImageConfig(BaseModel):
    """Configuration for a Bazel-built image pushed to Harbor.

    remote_path is the registry-relative path (e.g. ``oauth-broker/oauth-broker``).
    The registry host is defined once in generate_ci.py (HARBOR_REGISTRY).
    """

    bazel_target: str
    local_tag: str
    remote_path: str


class WorkflowManifest(BaseModel):
    """Collection of all workflow configurations."""

    workflows: dict[str, WorkflowConfig]
    releases: dict[str, ReleaseConfig] = Field(default_factory=dict)
    harbor_images: list[HarborImageConfig] = Field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: Path) -> WorkflowManifest:
        """Load from YAML file."""
        with path.open() as f:
            data = yaml.safe_load(f)
        return cls.model_validate(data)
