"""Materialize all agent system prompts as undeclared test outputs.

Renders each agent type's system prompt with a real database and writes
the result to TEST_UNDECLARED_OUTPUTS_DIR. This serves as:

1. A build-time smoke test — templates render without errors
2. A review artifact — rendered prompts can be inspected in CI output
3. A regression check — diffs in rendered output are visible in test logs
"""

from __future__ import annotations

import importlib.resources

import pytest_bazel

from props.agents.runtime import render_template_string
from props.core.agent_types import CriticDevImproveTypeConfig, CriticDevOptimizeTypeConfig, TargetMetric
from props.core.models.examples import WholeSnapshotExample
from props.db.database import Database
from util.testing.undeclared_outputs import undeclared_outputs_dir


def _render(db: Database, template_path: str, helpers: dict | None = None) -> str:
    """Render a template from package resources using the standard pipeline."""
    package, _, pkg_path = template_path.partition("/")
    content = (importlib.resources.files(package) / pkg_path).read_text()
    return render_template_string(content, db, helpers)


def _write_output(name: str, content: str) -> None:
    """Write rendered prompt to undeclared test outputs."""
    dest = undeclared_outputs_dir() / name
    dest.write_text(content)


def test_critic_prompt(db: Database):
    rendered = _render(
        db, "props/agents/critic/prompt.md.mako", helpers={"snapshot_slug": "test/snapshot-001", "scope_files": None}
    )
    _write_output("critic_whole_snapshot.md", rendered)
    assert "critic" in rendered.lower()
    assert "test/snapshot-001" in rendered
    assert "ALL files" in rendered
    assert "insert_issue" in rendered


def test_critic_prompt_scoped(db: Database):
    rendered = _render(
        db,
        "props/agents/critic/prompt.md.mako",
        helpers={"snapshot_slug": "test/snapshot-001", "scope_files": ["src/foo.py", "src/bar.py"]},
    )
    _write_output("critic_file_set.md", rendered)
    assert "src/foo.py" in rendered


def test_grader_prompt(db: Database):
    rendered = _render(db, "props/agents/grader/prompt.md.mako", helpers={"snapshot_slug": "test/snapshot-001"})
    _write_output("grader.md", rendered)
    assert "grader" in rendered.lower()
    assert "test/snapshot-001" in rendered
    assert "grading_edges" in rendered


def test_critic_dev_optimize_prompt(db: Database):
    rendered = _render(
        db,
        "props/agents/critic_dev/prompt.md.mako",
        helpers={
            "type_config": CriticDevOptimizeTypeConfig(
                target_metric=TargetMetric.WHOLE_REPO,
                optimizer_model="claude-opus-4-6",
                critic_model="claude-sonnet-4-6",
            )
        },
    )
    _write_output("critic_dev_optimize.md", rendered)
    assert "engineer" in rendered.lower()
    assert "recall" in rendered.lower()


def test_critic_dev_improve_prompt(db: Database):
    rendered = _render(
        db,
        "props/agents/critic_dev/prompt.md.mako",
        helpers={
            "type_config": CriticDevImproveTypeConfig(
                baseline_image_digests=["sha256:abc123"],
                allowed_examples=[WholeSnapshotExample(snapshot_slug="test/s")],
                improvement_model="claude-opus-4-6",
                critic_model="claude-sonnet-4-6",
            )
        },
    )
    _write_output("critic_dev_improve.md", rendered)
    assert "allowed_examples" in rendered


def test_describe_relation_in_prompts(db: Database):
    """Verify describe_relation produces schema content in rendered prompts."""
    rendered = _render(db, "props/agents/grader/prompt.md.mako", helpers={"snapshot_slug": "test/s"})
    # describe_relation outputs should contain column type info
    assert "grading_edges" in rendered
    assert "reported_issues" in rendered


if __name__ == "__main__":
    pytest_bazel.main()
