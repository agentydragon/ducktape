"""App-owned launch presets and their concrete resolution.

Preset names stop at this integration-app boundary. Kubernetes and the runner receive only the
resolved template, policies, bootstrap source, and SessionSpec fields.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Provider(StrEnum):
    """The native harness a ThreadPreset opens."""

    CLAUDE = "claude"
    CODEX = "codex"


class ThreadDefaults(BaseModel):
    """Editable thread launch fields; null means the preset or platform still supplies the field."""

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
            values["provider"] = f"PROVIDER_{str(provider).upper()}"
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
    policies: list[str] = Field(default_factory=list)
    thread_preset: str
    bootstrap: str = Field(default="", max_length=65_536)


class SandboxBinding(BaseModel):
    """The live preset association and only the operator's sandbox-level thread edits."""

    model_config = ConfigDict(extra="forbid")

    sandbox_preset: str
    thread_preset: str | None = None
    thread_overrides: ThreadDefaults = Field(default_factory=ThreadDefaults)


class SandboxPresetView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    title: str
    template: str
    policies: list[str]
    thread_preset: str
    thread_defaults: ThreadDefaults


class PresetCatalog(BaseModel):
    """Validated app configuration, keyed by stable names used in Sandbox annotations."""

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

    def sandbox(self, name: str) -> SandboxPreset:
        try:
            return self.sandboxes[name]
        except KeyError:
            raise UnknownPresetError("sandbox", name) from None

    def thread(self, name: str) -> ThreadPreset:
        try:
            return self.threads[name]
        except KeyError:
            raise UnknownPresetError("thread", name) from None

    def views(self) -> list[SandboxPresetView]:
        return [
            SandboxPresetView(
                name=name,
                title=preset.title,
                template=preset.template,
                policies=preset.policies,
                thread_preset=preset.thread_preset,
                thread_defaults=self.thread(preset.thread_preset).defaults(),
            )
            for name, preset in self.sandboxes.items()
        ]

    def thread_defaults(self, binding: SandboxBinding) -> ThreadDefaults:
        sandbox = self.sandbox(binding.sandbox_preset)
        selected = binding.thread_preset or sandbox.thread_preset
        return binding.thread_overrides.over(self.thread(selected).defaults())


class UnknownPresetError(Exception):
    def __init__(self, kind: str, name: str) -> None:
        super().__init__(f"unknown {kind} preset {name!r}")
        self.kind = kind
        self.name = name
