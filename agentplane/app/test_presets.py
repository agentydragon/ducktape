"""App-owned launch preset resolution, independent of Kubernetes and the runner."""

from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import ValidationError

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
                policy_sets=["github-agentydragon-agent"],
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


def _preset(**grants: list[str]) -> SandboxPreset:
    return SandboxPreset.model_validate({"title": "Misfit", "template": "runner", "thread_preset": "thread", **grants})


@pytest.mark.parametrize(
    "preset",
    [
        _preset(egress_policies=["github-agentydragon-agent"]),
        _preset(action_policy_sets=["github-reads"]),
        _preset(kubernetes_grants=["tool-config"]),
    ],
    ids=["egress", "action", "kubernetes"],
)
def test_a_preset_may_not_fill_fields_the_sandbox_service_refuses(preset: SandboxPreset) -> None:
    with pytest.raises(ValidationError, match="refuses"):
        PresetCatalog(
            sandboxes={"misfit": preset},
            threads={"thread": ThreadPreset(title="Thread", harness=Harness.CODEX, model="preset-model")},
        )


if __name__ == "__main__":
    pytest_bazel.main()
