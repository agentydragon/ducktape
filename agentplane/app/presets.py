"""App-owned form presets.

A preset is only a convenient collection of values the operator may choose individually. Kubernetes
and the runner receive the selected concrete template, egress policies, bootstrap source, and SessionSpec
fields, never a preset name to resolve later.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentplane.app.sandbox_models import SessionDefaults
from agentplane.runner.harness import Harness


class ThreadPreset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    harness: Harness
    model: str
    cwd: str = "/state/workspaces/{session_id}"
    reasoning_effort: str | None = None
    instructions: str = ""
    setup_script: str = Field(default="", max_length=65_536)

    def defaults(self) -> SessionDefaults:
        return SessionDefaults(
            harness=self.harness,
            model=self.model,
            cwd=self.cwd,
            reasoning_effort=self.reasoning_effort,
            instructions=self.instructions,
            setup_script=self.setup_script,
        )


class SandboxPreset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    template: str
    policy_sets: list[str] = Field(default_factory=list, description="PolicySet names every launch is prefilled with.")
    thread_preset: str
    bootstrap: str = Field(default="", max_length=65_536)


class SandboxPresetView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    title: str
    template: str
    policy_sets: list[str]
    session_defaults: SessionDefaults
    bootstrap: str


class PresetCatalog(BaseModel):
    """Validated app configuration, keyed by names the UI may expand into editable fields."""

    model_config = ConfigDict(extra="forbid")

    sandboxes: dict[str, SandboxPreset] = Field(default_factory=dict)
    threads: dict[str, ThreadPreset] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _references_exist(self) -> PresetCatalog:
        missing = {
            preset.thread_preset for preset in self.sandboxes.values() if preset.thread_preset not in self.threads
        }
        if missing:
            raise ValueError(f"SandboxPresets name unknown ThreadPresets: {sorted(missing)}")
        return self

    def views(self) -> list[SandboxPresetView]:
        return [
            SandboxPresetView(
                name=name,
                title=preset.title,
                template=preset.template,
                policy_sets=preset.policy_sets,
                session_defaults=self.threads[preset.thread_preset].defaults(),
                bootstrap=preset.bootstrap,
            )
            for name, preset in self.sandboxes.items()
        ]
