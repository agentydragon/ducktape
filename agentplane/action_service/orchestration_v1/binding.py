"""The reviewed configuration that binds the orchestration_v1 Actions to one deployment.

The Go executor reads it (catalog.OrchestrationBinding, presets.go); this model is what deployment
synthesis writes. Its own module, as the sandbox binding is, so `action_service.catalog` can name it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agentplane.env.runtime.harness import Harness


class PresetLaunch(BaseModel):
    """The `launch_sandbox` fields a preset stands for."""

    model_config = ConfigDict(extra="forbid")

    template: str = Field(min_length=1)
    policy_sets: list[str] = Field(default_factory=list)
    inbox_tags: list[str] = Field(default_factory=list)
    bootstrap: str = Field(default="", max_length=65_536)


class PresetSessionSpec(BaseModel):
    """The `create_session` spec a preset stands for."""

    model_config = ConfigDict(extra="forbid")

    harness: Harness
    model: str = Field(min_length=1)
    cwd: str = Field(min_length=1)
    reasoning_effort: str = ""
    instructions: str = ""


class PresetSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: PresetSessionSpec
    setup_script: str = Field(default="", max_length=65_536)


class LaunchPreset(BaseModel):
    """One reviewed preset, as the fields `list_launch_presets` answers with for a caller to copy."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    launch: PresetLaunch
    create_session: PresetSession


class OrchestrationExecutorBinding(BaseModel):
    """Session Sandboxes launched, and their Sessions driven, by this service on each request's
    Decision: a stopgap (`orchestration_v1/README.md`)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["orchestration_v1"] = "orchestration_v1"
    description: str = Field(min_length=1, max_length=2000, description="Agent-visible executor description.")
    namespace: str = Field(
        min_length=1, description="Namespace Sandboxes launch in: the Action Service's `policy_namespace`."
    )
    presets: dict[str, LaunchPreset] = Field(default_factory=dict)
