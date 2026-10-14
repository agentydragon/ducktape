"""App-owned form presets.

A Sandbox preset also names the configured session defaults the app applies when opening new Sessions.
Kubernetes stores only the preset label, concrete Sandbox template, policy sets, and bootstrap source.
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
    """Validated app configuration keyed by the Sandbox label stored on each Sandbox."""

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

    def defaults_for_sandbox(self, name: str | None) -> SessionDefaults:
        """Resolve launch defaults from the Sandbox's label, never from per-Sandbox state."""
        if name is None:
            return SessionDefaults()
        preset = self.sandboxes.get(name)
        if preset is None:
            raise KeyError(f"Sandbox names preset {name!r}, but it is not in the app's preset catalog")
        return self.threads[preset.thread_preset].defaults()
