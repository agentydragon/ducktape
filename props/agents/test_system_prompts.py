"""Materialize all agent system prompts as undeclared test outputs.

Renders each agent type's system prompt and writes the result to TEST_UNDECLARED_OUTPUTS_DIR. This serves as:

1. A build-time smoke test — templates render without errors
2. A review artifact — rendered prompts can be inspected in CI output
3. A regression check — diffs in rendered output are visible in test logs
"""

from __future__ import annotations

import pytest_bazel

from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from props.agents.runtime import render_system_prompt
from props.core.agent_types import CriticDevImproveTypeConfig, CriticDevOptimizeTypeConfig, TargetMetric
from props.core.ids import SnapshotSlug
from props.core.models.examples import WholeSnapshotExample


def _write_output(name: str, content: str) -> None:
    """Write rendered prompt to undeclared test outputs."""
    dest = undeclared_outputs_dir() / name
    dest.write_text(content)


def test_critic_prompt():
    rendered = render_system_prompt(
        "props/agents/critic/prompt.md.mako", helpers={"snapshot_slug": "test/snapshot-001", "scope_files": None}
    )
    _write_output("critic_whole_snapshot.md", rendered)
    assert "test/snapshot-001" in rendered


def test_critic_prompt_scoped():
    rendered = render_system_prompt(
        "props/agents/critic/prompt.md.mako",
        helpers={"snapshot_slug": "test/snapshot-001", "scope_files": ["src/foo.py", "src/bar.py"]},
    )
    _write_output("critic_file_set.md", rendered)
    assert "src/foo.py" in rendered


def test_grader_prompt():
    rendered = render_system_prompt(
        "props/agents/grader/prompt.md.mako", helpers={"snapshot_slug": "test/snapshot-001"}
    )
    _write_output("grader.md", rendered)
    assert "test/snapshot-001" in rendered


def test_critic_dev_optimize_prompt():
    rendered = render_system_prompt(
        "props/agents/critic_dev/prompt.md.mako",
        helpers={
            "type_config": CriticDevOptimizeTypeConfig(
                target_metric=TargetMetric.WHOLE_REPO, optimizer_model="claude-opus-5", critic_model="claude-sonnet-5"
            )
        },
    )
    _write_output("critic_dev_optimize.md", rendered)


def test_critic_dev_improve_prompt():
    rendered = render_system_prompt(
        "props/agents/critic_dev/prompt.md.mako",
        helpers={
            "type_config": CriticDevImproveTypeConfig(
                baseline_image_digests=["sha256:abc123"],
                allowed_examples=[WholeSnapshotExample(snapshot_slug=SnapshotSlug("test/s"))],
                improvement_model="claude-opus-5",
                critic_model="claude-sonnet-5",
            )
        },
    )
    _write_output("critic_dev_improve.md", rendered)


if __name__ == "__main__":
    pytest_bazel.main()
