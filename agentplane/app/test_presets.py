"""App-owned launch preset resolution, independent of Kubernetes and the runner."""

from __future__ import annotations

import pytest
import pytest_bazel

from agentplane.app.presets import PresetCatalog, SandboxPreset, ThreadPreset
from agentplane.app.sandbox_models import SessionDefaults
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


@pytest.mark.parametrize(
    ("edit", "instructions"),
    [(SessionDefaults(instructions=""), ""), (SessionDefaults(), "preset instructions")],
    ids=["explicit-empty-wins", "unset-keeps-preset"],
)
def test_session_defaults_over_replaces_only_explicitly_set_fields(
    presets: PresetCatalog, edit: SessionDefaults, instructions: str
) -> None:
    [selected] = presets.views()

    merged = edit.over(selected.session_defaults)

    assert (merged.model, merged.instructions) == ("preset-model", instructions)


if __name__ == "__main__":
    pytest_bazel.main()
