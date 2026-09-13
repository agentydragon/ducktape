"""App-owned form presets.

A preset is only a convenient collection of values the operator may choose individually. Kubernetes
and the runner receive the selected concrete template, policies, bootstrap source, and SessionSpec
fields, never a preset name to resolve later.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Provider(StrEnum):
    """The runner protocol Provider enum names, reused by configuration and Thread projections."""

    CLAUDE = "PROVIDER_CLAUDE"
    CODEX = "PROVIDER_CODEX"


class ThreadDefaults(BaseModel):
    """Editable SessionSpec launch fields; null means the caller deliberately left that field unspecified."""

    model_config = ConfigDict(extra="forbid")

    provider: Provider | None = None
    model: str | None = None
    cwd: str | None = None
    reasoning_effort: str | None = None
    instructions: str | None = None

    def over(self, base: ThreadDefaults) -> ThreadDefaults:
        """Replace only fields explicitly present in this object, including an explicit empty string."""
        return base.model_copy(update=self.model_dump(exclude_none=True))

    def proto_json(self, session_id: str) -> dict[str, object]:
        values = self.model_dump(exclude_none=True)
        if cwd := values.get("cwd"):
            values["cwd"] = str(cwd).replace("{session_id}", session_id)
        if provider := values.pop("provider", None):
            values["provider"] = str(provider)
        if "reasoning_effort" in values:
            values["reasoningEffort"] = values.pop("reasoning_effort")
        return values


class ThreadPreset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    provider: Provider
    model: str
    cwd: str = "/state/workspaces/{session_id}"
    reasoning_effort: str = "low"
    instructions: str = ""

    def defaults(self) -> ThreadDefaults:
        return ThreadDefaults.model_validate(self.model_dump(exclude={"title"}))


class SandboxPreset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    template: str
    policies: list[str] = Field(default_factory=list, description="EgressPolicy names every launch is granted.")
    action_policy_sets: list[str] = Field(
        default_factory=list,
        description="ActionPolicySet names every launch is bound to: what its harness may do without the operator.",
    )
    thread_preset: str
    bootstrap: str = Field(default="", max_length=65_536)


class SandboxBinding(BaseModel):
    """The exact reusable Thread defaults and bootstrap the Sandbox was created with."""

    model_config = ConfigDict(extra="forbid")

    thread_defaults: ThreadDefaults | None = None
    bootstrap: str = Field(max_length=65_536)


class SandboxPresetView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    title: str
    template: str
    policies: list[str]
    action_policy_sets: list[str]
    thread_defaults: ThreadDefaults
    bootstrap: str


class PresetCatalog(BaseModel):
    """Validated app configuration, keyed by names the UI may expand into editable fields."""

    model_config = ConfigDict(extra="forbid")

    sandboxes: dict[str, SandboxPreset] = Field(default_factory=dict)
    threads: dict[str, ThreadPreset] = Field(default_factory=dict)
    agent_instructions: str = Field(
        default="", description="Operational instructions prepended to every Agentplane-launched session."
    )

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
                policies=preset.policies,
                action_policy_sets=preset.action_policy_sets,
                thread_defaults=self.threads[preset.thread_preset].defaults(),
                bootstrap=preset.bootstrap,
            )
            for name, preset in self.sandboxes.items()
        ]

    def instructions_for(self, task_instructions: str) -> str:
        """Combine platform operation guidance with the caller's task-specific instructions."""
        parts = [part.strip() for part in (self.agent_instructions, task_instructions) if part.strip()]
        return "\n\n".join(parts)
