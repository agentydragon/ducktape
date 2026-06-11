"""Tests for agent_types module."""

from uuid import UUID

import pytest
import pytest_bazel
from pydantic import TypeAdapter, ValidationError

from props.core.agent_types import (
    AgentConfig,
    AgentType,
    CriticDevImproveTypeConfig,
    CriticDevOptimizeTypeConfig,
    CriticTypeConfig,
    FreeformTypeConfig,
    GraderTypeConfig,
    TypeConfig,
)
from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleSpec, WholeSnapshotExample
from props.core.oci_utils import BUILTIN_TAG

TEST_SLUG = SnapshotSlug("test/2025-01-01-00")
TEST_EXAMPLE = WholeSnapshotExample(snapshot_slug=TEST_SLUG)
TEST_DIGEST = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def _improvement_config(
    *, baseline_image_digests: list[str] | None = None, allowed_examples: list[ExampleSpec] | None = None
) -> CriticDevImproveTypeConfig:
    return CriticDevImproveTypeConfig(
        baseline_image_digests=baseline_image_digests if baseline_image_digests is not None else [TEST_DIGEST],
        allowed_examples=allowed_examples if allowed_examples is not None else [TEST_EXAMPLE],
        improvement_model="test-model",
        critic_model="test-critic-model",
    )


@pytest.fixture
def type_config_adapter() -> TypeAdapter[TypeConfig]:
    """TypeAdapter for discriminated union parsing."""
    return TypeAdapter(TypeConfig)


class TestTypeConfigDiscriminatedUnion:
    """Tests for TypeConfig discriminated union parsing."""

    @pytest.mark.parametrize(
        ("data", "expected_type"),
        [
            (
                {"agent_type": "critic", "example": {"kind": "whole_snapshot", "snapshot_slug": "test/2025-01-01-00"}},
                CriticTypeConfig,
            ),
            ({"agent_type": "grader", "snapshot_slug": "test/2025-01-01-00"}, GraderTypeConfig),
            ({"agent_type": "freeform"}, FreeformTypeConfig),
            (
                {
                    "agent_type": "critic_dev_optimize",
                    "target_metric": "whole-repo",
                    "optimizer_model": "test-optimizer",
                    "critic_model": "test-critic",
                },
                CriticDevOptimizeTypeConfig,
            ),
            (
                {
                    "agent_type": "critic_dev_improve",
                    "baseline_image_digests": [TEST_DIGEST],
                    "allowed_examples": [{"kind": "whole_snapshot", "snapshot_slug": "test/2025-01-01-00"}],
                    "improvement_model": "test-improvement",
                    "critic_model": "test-critic",
                },
                CriticDevImproveTypeConfig,
            ),
        ],
    )
    def test_discriminator_routes_to_correct_type(
        self, type_config_adapter: TypeAdapter[TypeConfig], data: dict, expected_type: type
    ) -> None:
        """Discriminated union routes to correct config type based on agent_type."""
        config = type_config_adapter.validate_python(data)
        assert isinstance(config, expected_type)

    def test_invalid_agent_type_rejected(self, type_config_adapter: TypeAdapter[TypeConfig]) -> None:
        """Unknown agent_type values are rejected."""
        with pytest.raises(ValidationError):
            type_config_adapter.validate_python({"agent_type": "invalid"})


class TestGraderTypeConfig:
    """Tests for GraderTypeConfig behavior (grader model with snapshot_slug)."""

    def test_valid_construction(self) -> None:
        """GraderTypeConfig accepts valid snapshot_slug."""
        config = GraderTypeConfig(snapshot_slug=TEST_SLUG)
        assert config.snapshot_slug == TEST_SLUG
        assert config.agent_type == AgentType.GRADER

    def test_snapshot_slug_required(self) -> None:
        """snapshot_slug is required."""
        with pytest.raises(ValidationError):
            GraderTypeConfig()  # type: ignore[call-arg]


class TestCriticDevImproveTypeConfig:
    """Tests for CriticDevImproveTypeConfig behavior."""

    def test_valid_construction(self) -> None:
        """CriticDevImproveTypeConfig accepts valid data."""
        config = _improvement_config()
        assert config.baseline_image_digests == [TEST_DIGEST]
        assert len(config.allowed_examples) == 1
        assert config.agent_type == AgentType.CRITIC_DEV_IMPROVE
        assert config.improvement_model == "test-model"
        assert config.critic_model == "test-critic-model"

    def test_baseline_image_digests_required_nonempty(self) -> None:
        """baseline_image_digests must have at least one element."""
        with pytest.raises(ValidationError, match="at least 1"):
            _improvement_config(baseline_image_digests=[])

    def test_allowed_examples_required_nonempty(self) -> None:
        """allowed_examples must have at least one element."""
        with pytest.raises(ValidationError, match="at least 1"):
            _improvement_config(allowed_examples=[])

    def test_multiple_image_refs_allowed(self) -> None:
        """Multiple baseline image refs can be provided."""
        config = _improvement_config(
            baseline_image_digests=[
                "sha256:aaaa000000000000000000000000000000000000000000000000000000000001",
                "sha256:bbbb000000000000000000000000000000000000000000000000000000000002",
                "sha256:cccc000000000000000000000000000000000000000000000000000000000003",
            ]
        )
        assert len(config.baseline_image_digests) == 3

    def test_multiple_examples_allowed(self) -> None:
        """Multiple allowed examples can be provided."""
        config = _improvement_config(
            allowed_examples=[
                WholeSnapshotExample(snapshot_slug=TEST_SLUG),
                WholeSnapshotExample(snapshot_slug=SnapshotSlug("test/2025-01-02-00")),
            ]
        )
        assert len(config.allowed_examples) == 2


class TestAgentConfig:
    """Tests for AgentConfig combining shared fields with type-specific config."""

    def test_basic_construction_with_critic(self) -> None:
        """AgentConfig accepts all required fields with CriticTypeConfig."""
        config = AgentConfig(
            image_ref=BUILTIN_TAG, model="claude-sonnet-4-6", type_config=CriticTypeConfig(example=TEST_EXAMPLE)
        )
        assert config.image_ref == BUILTIN_TAG
        assert config.model == "claude-sonnet-4-6"
        assert config.parent_agent_run_id is None
        assert isinstance(config.type_config, CriticTypeConfig)

    @pytest.mark.parametrize(
        "type_config",
        [
            CriticTypeConfig(example=WholeSnapshotExample(snapshot_slug=SnapshotSlug("test/2025-01-01-00"))),
            GraderTypeConfig(snapshot_slug=SnapshotSlug("test/2025-01-01-00")),
            FreeformTypeConfig(),
        ],
        ids=lambda tc: tc.agent_type,
    )
    def test_agent_type_property_delegates_to_type_config(self, type_config: TypeConfig) -> None:
        """agent_type property delegates to type_config.agent_type."""
        config = AgentConfig(image_ref="test", model="claude-sonnet-4-6", type_config=type_config)
        assert config.agent_type == type_config.agent_type

    def test_parent_agent_run_id_accepts_uuid(self) -> None:
        """parent_agent_run_id accepts UUID for sub-agents."""
        parent_id = UUID("550e8400-e29b-41d4-a716-446655440000")
        config = AgentConfig(
            image_ref="freeform",
            model="claude-sonnet-4-6",
            parent_agent_run_id=parent_id,
            type_config=FreeformTypeConfig(),
        )
        assert config.parent_agent_run_id == parent_id

    def test_parent_agent_run_id_coerced_from_string(self) -> None:
        """parent_agent_run_id is coerced from string to UUID."""
        config = AgentConfig(
            image_ref="freeform",
            model="claude-sonnet-4-6",
            parent_agent_run_id="550e8400-e29b-41d4-a716-446655440000",
            type_config=FreeformTypeConfig(),
        )
        assert isinstance(config.parent_agent_run_id, UUID)

    def test_json_serialization_roundtrip(self) -> None:
        """AgentConfig can be serialized to JSON and back."""
        original = AgentConfig(
            image_ref=BUILTIN_TAG,
            model="claude-sonnet-4-6",
            parent_agent_run_id=UUID("550e8400-e29b-41d4-a716-446655440000"),
            type_config=CriticTypeConfig(example=TEST_EXAMPLE),
        )
        json_str = original.model_dump_json()
        restored = AgentConfig.model_validate_json(json_str)
        assert restored == original
        assert restored.agent_type == AgentType.CRITIC


if __name__ == "__main__":
    pytest_bazel.main()
