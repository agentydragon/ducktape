"""Tests for agent_types module."""

from uuid import UUID

import pytest_bazel

from props.core.agent_types import AgentConfig, AgentType, CriticTypeConfig
from props.core.ids import SnapshotSlug
from props.core.models.examples import WholeSnapshotExample
from props.core.oci_utils import BUILTIN_TAG

TEST_EXAMPLE = WholeSnapshotExample(snapshot_slug=SnapshotSlug("test/2025-01-01-00"))


def test_json_serialization_roundtrip() -> None:
    """AgentConfig survives the JSON roundtrip it takes through agent_runs.type_config."""
    original = AgentConfig(
        image_ref=BUILTIN_TAG,
        model="claude-sonnet-5",
        parent_agent_run_id=UUID("550e8400-e29b-41d4-a716-446655440000"),
        type_config=CriticTypeConfig(example=TEST_EXAMPLE),
    )
    restored = AgentConfig.model_validate_json(original.model_dump_json())
    assert restored == original
    assert restored.agent_type == AgentType.CRITIC


if __name__ == "__main__":
    pytest_bazel.main()
