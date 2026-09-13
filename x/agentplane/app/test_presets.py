"""App-owned launch preset resolution, independent of Kubernetes and the runner."""

from __future__ import annotations

import pytest
import pytest_bazel

from x.agentplane.app.presets import PresetCatalog, SandboxBinding, SandboxPreset, ThreadDefaults, ThreadPreset


@pytest.fixture
def presets() -> PresetCatalog:
    return PresetCatalog(
        sandboxes={
            "public-coder": SandboxPreset(
                title="Public coder",
                template="runner",
                policies=["github-public"],
                thread_preset="public-coder-codex",
                bootstrap="mkdir -p /state/workspaces",
            )
        },
        threads={
            "public-coder-codex": ThreadPreset(
                title="Public coder / Codex",
                provider="codex",
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
        "policies": ["github-public"],
        "action_policy_sets": [],
        "thread_defaults": {
            "provider": "PROVIDER_CODEX",
            "model": "preset-model",
            "cwd": "/state/workspaces/{session_id}",
            "reasoning_effort": "medium",
            "instructions": "preset instructions",
        },
        "bootstrap": "mkdir -p /state/workspaces",
    }


def test_sandbox_binding_keeps_the_selected_values_when_the_catalog_changes(presets: PresetCatalog) -> None:
    [selected] = presets.views()
    binding = SandboxBinding(
        thread_defaults=ThreadDefaults(instructions="").over(selected.thread_defaults), bootstrap=selected.bootstrap
    )
    presets.threads["public-coder-codex"] = presets.threads["public-coder-codex"].model_copy(
        update={"model": "new-preset-model", "reasoning_effort": "high"}
    )

    assert binding.thread_defaults is not None
    assert binding.thread_defaults.model_dump() == {
        "provider": "PROVIDER_CODEX",
        "model": "preset-model",
        "cwd": "/state/workspaces/{session_id}",
        "reasoning_effort": "medium",
        "instructions": "",
    }
    assert binding.thread_defaults.proto_json("thread-7") == {
        "provider": "PROVIDER_CODEX",
        "model": "preset-model",
        "cwd": "/state/workspaces/thread-7",
        "reasoningEffort": "medium",
        "instructions": "",
    }


def test_shared_agent_instructions_precede_the_task_without_replacing_it() -> None:
    catalog = PresetCatalog(agent_instructions="platform instructions")

    assert catalog.instructions_for("task instructions") == "platform instructions\n\ntask instructions"
    assert catalog.instructions_for("") == "platform instructions"


if __name__ == "__main__":
    pytest_bazel.main()
