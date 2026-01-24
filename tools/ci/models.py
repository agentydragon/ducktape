"""Shared models for CI scripts.

Contains Pydantic models used by both ci_decide.py and generate_ci.py.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class WorkflowConfig(BaseModel):
    """Configuration for a workflow from workflows.yaml.

    Used by both the decision engine (to determine triggers) and the
    generator (to produce job YAML).
    """

    bazel_pattern: str | None = None
    path_pattern: str | None = None
    always: bool = False
    # Generation-only fields
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
        workflows = {name: WorkflowConfig.model_validate(config) for name, config in data.items()}
        return cls(workflows=workflows)
