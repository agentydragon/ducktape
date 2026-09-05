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


def test_sandbox_overrides_replace_only_named_thread_defaults(presets: PresetCatalog) -> None:
    binding = SandboxBinding(
        sandbox_preset="public-coder", thread_overrides=ThreadDefaults(model="edited-model", instructions="")
    )

    resolved = presets.thread_defaults(binding)

    assert resolved.model_dump() == {
        "provider": "codex",
        "model": "edited-model",
        "cwd": "/state/workspaces/{session_id}",
        "reasoning_effort": "medium",
        "instructions": "",
    }
    assert resolved.proto_json("thread-7") == {
        "provider": "PROVIDER_CODEX",
        "model": "edited-model",
        "cwd": "/state/workspaces/thread-7",
        "reasoningEffort": "medium",
        "instructions": "",
    }


def test_a_changed_preset_remains_live_behind_explicit_overrides(presets: PresetCatalog) -> None:
    binding = SandboxBinding(
        sandbox_preset="public-coder", thread_overrides=ThreadDefaults(instructions="sandbox instruction")
    )
    presets.threads["public-coder-codex"] = presets.threads["public-coder-codex"].model_copy(
        update={"model": "new-preset-model", "reasoning_effort": "high"}
    )

    resolved = presets.thread_defaults(binding)

    assert (resolved.model, resolved.reasoning_effort, resolved.instructions) == (
        "new-preset-model",
        "high",
        "sandbox instruction",
    )


if __name__ == "__main__":
    pytest_bazel.main()
