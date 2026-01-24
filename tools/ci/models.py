"""Shared models for CI scripts.

Contains Pydantic models used by both ci_decide.py and generate_ci.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


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


WorkflowTrigger = AlwaysTrigger | BazelPatternTrigger | PathPatternTrigger


class WorkflowConfig(BaseModel):
    """Configuration for a workflow from workflows.yaml."""

    trigger: WorkflowTrigger
    targets: bool = False
    inputs: dict[str, str] = Field(default_factory=dict)
    secrets: list[str] = Field(default_factory=list)

    @classmethod
    def from_yaml_dict(cls, data: dict) -> WorkflowConfig:
        """Parse workflow config from YAML dict format."""
        if data.get("always"):
            trigger: WorkflowTrigger = AlwaysTrigger()
        elif pattern := data.get("bazel_pattern"):
            trigger = BazelPatternTrigger(pattern=pattern)
        elif pattern := data.get("path_pattern"):
            trigger = PathPatternTrigger(pattern=pattern)
        else:
            raise ValueError("Workflow must have always, bazel_pattern, or path_pattern")

        return cls(
            trigger=trigger,
            targets=data.get("targets", False),
            inputs=data.get("inputs", {}),
            secrets=data.get("secrets", []),
        )


class WorkflowManifest(BaseModel):
    """Collection of all workflow configurations."""

    workflows: dict[str, WorkflowConfig]

    @classmethod
    def from_yaml(cls, path: Path) -> WorkflowManifest:
        """Load from YAML file."""
        with path.open() as f:
            data = yaml.safe_load(f)
        workflows = {name: WorkflowConfig.from_yaml_dict(config) for name, config in data.items()}
        return cls(workflows=workflows)
