"""The launch preset `test_launch_presets` exercises. The k3s fixture configures it in place of the
deployment's own presets, so the scenario never depends on a preset an operator uses."""

from __future__ import annotations

from agentplane.app.presets import SandboxPreset, ThreadPreset
from agentplane.env.runtime.harnesshost.harness import Harness

SANDBOX_PRESET = "test-scripted-sandbox-preset"
THREAD_PRESET = "test-scripted-thread-preset"
# The file the preset's bootstrap writes, and what it writes there.
READY_MARKER = "/state/workspaces/.test-scripted-preset-ready"
READY_TEXT = "test scripted preset workspace initialized"


def thread_preset(*, model: str) -> ThreadPreset:
    return ThreadPreset(
        title="Test scripted thread preset",
        harness=Harness.CODEX,
        model=model,
        reasoning_effort="medium",
        instructions="Test scripted thread preset instructions.",
    )


def sandbox_preset(*, template: str, policy_sets: list[str]) -> SandboxPreset:
    return SandboxPreset(
        title="Test scripted sandbox preset",
        template=template,
        policy_sets=policy_sets,
        thread_preset=THREAD_PRESET,
        bootstrap=f"mkdir -p /state/workspaces\nprintf '%s\\n' '{READY_TEXT}' > {READY_MARKER}\n",
    )
