"""App-owned launch preset resolution, independent of Kubernetes and the runner."""

from __future__ import annotations

import pytest
import pytest_bazel

from agentplane.app.presets import PresetCatalog, SandboxPreset, ThreadPreset
from agentplane.app.sandbox_models import SandboxBinding, SessionDefaults
from agentplane.runner.harness import Harness


@pytest.fixture
def presets() -> PresetCatalog:
    return PresetCatalog(
        sandboxes={
            "public-coder": SandboxPreset(
                title="Public coder",
                template="runner",
                policies=["github-agentydragon-agent"],
                thread_preset="public-coder-codex",
                bootstrap="mkdir -p /state/workspaces",
            )
        },
        threads={
            "public-coder-codex": ThreadPreset(
                title="Public coder / Codex",
                harness=Harness.CODEX,
                model="preset-model",
                reasoning_effort="medium",
                instructions="preset instructions",
            )
        },
    )


def test_sandbox_preset_expands_to_fields_the_operator_can_set_individually(presets: PresetCatalog) -> None:
    [view] = presets.views()

    assert view.model_dump() == {
        "name": "public-coder",
        "title": "Public coder",
        "template": "runner",
        "policies": ["github-agentydragon-agent"],
        "action_policy_sets": [],
        "kubernetes_grants": [],
        "session_defaults": {
            "harness": "HARNESS_CODEX",
            "model": "preset-model",
            "cwd": "/state/workspaces/{session_id}",
            "reasoning_effort": "medium",
            "instructions": "preset instructions",
            "setup_script": "",
        },
        "bootstrap": "mkdir -p /state/workspaces",
    }


def test_sandbox_binding_keeps_the_selected_values_when_the_catalog_changes(presets: PresetCatalog) -> None:
    [selected] = presets.views()
    binding = SandboxBinding(
        session_defaults=SessionDefaults(instructions="").over(selected.session_defaults), bootstrap=selected.bootstrap
    )
    presets.threads["public-coder-codex"] = presets.threads["public-coder-codex"].model_copy(
        update={"model": "new-preset-model", "reasoning_effort": "high"}
    )

    assert binding.session_defaults is not None
    assert binding.session_defaults.model_dump() == {
        "harness": "HARNESS_CODEX",
        "model": "preset-model",
        "cwd": "/state/workspaces/{session_id}",
        "reasoning_effort": "medium",
        "instructions": "",
        "setup_script": "",
    }


if __name__ == "__main__":
    pytest_bazel.main()
