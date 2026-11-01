"""App-owned form presets.

A Sandbox preset also names the configured session defaults the app applies when opening new Sessions.
Kubernetes stores only the preset label, concrete Sandbox template, policy sets, inbox tags, and bootstrap source.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from agentplane.app.sandbox_models import InboxTag
from agentplane.env.runtime.harnesshost.harness import Harness


class ThreadPreset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    harness: Harness
    model: str
    cwd: str = "/state/workspaces/{session_id}"
    reasoning_effort: str | None = None
    instructions: str = ""
    setup_script: str = Field(default="", max_length=65_536)


class SandboxPreset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    template: str
    policy_sets: list[str] = Field(default_factory=list, description="PolicySet names every launch is prefilled with.")
    inbox_tags: list[InboxTag] = Field(default_factory=list, description="Inbox tags every launch is prefilled with.")
    thread_preset: str
    bootstrap: str = Field(default="", max_length=65_536)
