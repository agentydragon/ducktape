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
    egress_policies: list[str] = Field(default_factory=list, description="EgressPolicy names every launch is granted.")
    action_policy_sets: list[str] = Field(
        default_factory=list,
        description="ActionPolicySet names every launch is bound to: what its harness may do without the operator.",
    )
    kubernetes_grants: list[str] = Field(
        default_factory=list, description="Enabled Kubernetes grant names to prefill at Sandbox launch."
    )
    policy_sets: list[str] = Field(
        default_factory=list,
        description="PolicySet names to prefill, where Sandboxes are Agentplane's own kind, in place of the "
        "three lists above.",
    )
    thread_preset: str
    bootstrap: str = Field(default="", max_length=65_536)


class SandboxPresetView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    title: str
    template: str
    egress_policies: list[str]
    action_policy_sets: list[str]
    kubernetes_grants: list[str]
    policy_sets: list[str]
    session_defaults: SessionDefaults
    bootstrap: str


class PresetCatalog(BaseModel):
    """Validated app configuration, keyed by names the UI may expand into editable fields."""

    model_config = ConfigDict(extra="forbid")

    sandboxes: dict[str, SandboxPreset] = Field(default_factory=dict)
    threads: dict[str, ThreadPreset] = Field(default_factory=dict)
    own_sandboxes: bool = Field(
        default=False,
        description="The Sandbox Service creates Agentplane's own Sandbox kind, which takes policy sets alone.",
    )

    @model_validator(mode="after")
    def _references_exist(self) -> PresetCatalog:
        missing = {
            preset.thread_preset for preset in self.sandboxes.values() if preset.thread_preset not in self.threads
        }
        if missing:
            raise ValueError(f"SandboxPresets name unknown ThreadPresets: {sorted(missing)}")
        return self

    @model_validator(mode="after")
    def _fields_fit_the_kind(self) -> PresetCatalog:
        """A preset fills only the fields the Sandbox Service accepts for the kind it creates."""
        misfit = sorted(
            name
            for name, preset in self.sandboxes.items()
            if (
                bool(preset.egress_policies or preset.action_policy_sets or preset.kubernetes_grants)
                if self.own_sandboxes
                else bool(preset.policy_sets)
            )
        )
        if misfit:
            raise ValueError(f"SandboxPresets fill fields their Sandbox kind refuses: {misfit}")
        return self

    def views(self) -> list[SandboxPresetView]:
        return [
            SandboxPresetView(
                name=name,
                title=preset.title,
                template=preset.template,
                egress_policies=preset.egress_policies,
                action_policy_sets=preset.action_policy_sets,
                kubernetes_grants=preset.kubernetes_grants,
                policy_sets=preset.policy_sets,
                session_defaults=self.threads[preset.thread_preset].defaults(),
                bootstrap=preset.bootstrap,
            )
            for name, preset in self.sandboxes.items()
        ]
