"""Tests for agent_types module."""

from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from props.core.agent_types import (
    AgentConfig,
    AgentType,
    CriticTypeConfig,
    FreeformTypeConfig,
    GraderTypeConfig,
    ImprovementTypeConfig,
    PromptOptimizerTypeConfig,
    TypeConfig,
)
from props.core.db.agent_definition_ids import CRITIC_IMAGE_REF
from props.core.ids import SnapshotSlug
from props.core.models.examples import WholeSnapshotExample


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
            ({"agent_type": "grader", "graded_agent_run_id": "550e8400-e29b-41d4-a716-446655440000"}, GraderTypeConfig),
            ({"agent_type": "freeform"}, FreeformTypeConfig),
            (
                {
                    "agent_type": "prompt_optimizer",
                    "target_metric": "whole-repo",
                    "optimizer_model": "test-optimizer",
                    "critic_model": "test-critic",
                    "grader_model": "test-grader",
                    "budget_limit": 100.0,
                },
                PromptOptimizerTypeConfig,
            ),
            (
                {
                    "agent_type": "improvement",
                    "baseline_image_refs": ["critic-v1"],
                    "allowed_examples": [{"kind": "whole_snapshot", "snapshot_slug": "test/2025-01-01-00"}],
                    "improvement_model": "test-improvement",
                    "critic_model": "test-critic",
                    "grader_model": "test-grader",
                },
                ImprovementTypeConfig,
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
    """Tests for GraderTypeConfig behavior."""

    def test_uuid_coercion_from_string(self) -> None:
        """Pydantic coerces string to UUID."""
        config = GraderTypeConfig(graded_agent_run_id="550e8400-e29b-41d4-a716-446655440000")
        assert isinstance(config.graded_agent_run_id, UUID)

    def test_canonical_issues_snapshot_optional(self) -> None:
        """canonical_issues_snapshot defaults to None."""
        config = GraderTypeConfig(graded_agent_run_id=UUID("550e8400-e29b-41d4-a716-446655440000"))
        assert config.canonical_issues_snapshot is None

    def test_canonical_issues_snapshot_accepts_dict(self) -> None:
        """canonical_issues_snapshot accepts dict value."""
        config = GraderTypeConfig(
            graded_agent_run_id=UUID("550e8400-e29b-41d4-a716-446655440000"),
            canonical_issues_snapshot={"true_positives": [], "false_positives": []},
        )
        assert config.canonical_issues_snapshot == {"true_positives": [], "false_positives": []}


class TestImprovementTypeConfig:
    """Tests for ImprovementTypeConfig behavior."""

    def test_valid_construction(self) -> None:
        """ImprovementTypeConfig accepts valid data."""
        config = ImprovementTypeConfig(
            baseline_image_refs=["critic-v1"],
            allowed_examples=[WholeSnapshotExample(snapshot_slug=SnapshotSlug("test/2025-01-01-00"))],
            improvement_model="test-model",
            critic_model="test-critic-model",
            grader_model="test-grader-model",
        )
        assert config.baseline_image_refs == ["critic-v1"]
        assert len(config.allowed_examples) == 1
        assert config.agent_type == AgentType.IMPROVEMENT
        assert config.improvement_model == "test-model"
        assert config.critic_model == "test-critic-model"
        assert config.grader_model == "test-grader-model"

    def test_baseline_image_refs_required_nonempty(self) -> None:
        """baseline_image_refs must have at least one element."""
        with pytest.raises(ValidationError, match="at least 1"):
            ImprovementTypeConfig(
                baseline_image_refs=[],
                allowed_examples=[WholeSnapshotExample(snapshot_slug=SnapshotSlug("test/2025-01-01-00"))],
                improvement_model="test-model",
                critic_model="test-critic-model",
                grader_model="test-grader-model",
            )

    def test_allowed_examples_required_nonempty(self) -> None:
        """allowed_examples must have at least one element."""
        with pytest.raises(ValidationError, match="at least 1"):
            ImprovementTypeConfig(
                baseline_image_refs=["critic-v1"],
                allowed_examples=[],
                improvement_model="test-model",
                critic_model="test-critic-model",
                grader_model="test-grader-model",
            )

    def test_multiple_image_refs_allowed(self) -> None:
        """Multiple baseline image refs can be provided."""
        config = ImprovementTypeConfig(
            baseline_image_refs=["critic-v1", "critic-v2", "critic-experimental"],
            allowed_examples=[WholeSnapshotExample(snapshot_slug=SnapshotSlug("test/2025-01-01-00"))],
            improvement_model="test-model",
            critic_model="test-critic-model",
            grader_model="test-grader-model",
        )
        assert len(config.baseline_image_refs) == 3

    def test_multiple_examples_allowed(self) -> None:
        """Multiple allowed examples can be provided."""
        config = ImprovementTypeConfig(
            baseline_image_refs=["critic-v1"],
            allowed_examples=[
                WholeSnapshotExample(snapshot_slug=SnapshotSlug("test/2025-01-01-00")),
                WholeSnapshotExample(snapshot_slug=SnapshotSlug("test/2025-01-02-00")),
            ],
            improvement_model="test-model",
            critic_model="test-critic-model",
            grader_model="test-grader-model",
        )
        assert len(config.allowed_examples) == 2


class TestAgentConfig:
    """Tests for AgentConfig combining shared fields with type-specific config."""

    def test_basic_construction_with_critic(self) -> None:
        """AgentConfig accepts all required fields with CriticTypeConfig."""
        config = AgentConfig(
            image_ref=CRITIC_IMAGE_REF,
            model="claude-sonnet-4-20250514",
            type_config=CriticTypeConfig(
                example=WholeSnapshotExample(snapshot_slug=SnapshotSlug("test/2025-01-01-00"))
            ),
        )
        assert config.image_ref == CRITIC_IMAGE_REF
        assert config.model == "claude-sonnet-4-20250514"
        assert config.parent_agent_run_id is None
        assert isinstance(config.type_config, CriticTypeConfig)

    @pytest.mark.parametrize(
        "type_config",
        [
            CriticTypeConfig(example=WholeSnapshotExample(snapshot_slug=SnapshotSlug("test/2025-01-01-00"))),
            GraderTypeConfig(graded_agent_run_id=UUID("550e8400-e29b-41d4-a716-446655440000")),
            FreeformTypeConfig(),
        ],
        ids=lambda tc: tc.agent_type,
    )
    def test_agent_type_property_delegates_to_type_config(self, type_config: TypeConfig) -> None:
        """agent_type property delegates to type_config.agent_type."""
        config = AgentConfig(image_ref="test", model="claude-sonnet-4-20250514", type_config=type_config)
        assert config.agent_type == type_config.agent_type

    def test_parent_agent_run_id_accepts_uuid(self) -> None:
        """parent_agent_run_id accepts UUID for sub-agents."""
        parent_id = UUID("550e8400-e29b-41d4-a716-446655440000")
        config = AgentConfig(
            image_ref="freeform",
            model="claude-sonnet-4-20250514",
            parent_agent_run_id=parent_id,
            type_config=FreeformTypeConfig(),
        )
        assert config.parent_agent_run_id == parent_id

    def test_parent_agent_run_id_coerced_from_string(self) -> None:
        """parent_agent_run_id is coerced from string to UUID."""
        config = AgentConfig(
            image_ref="freeform",
            model="claude-sonnet-4-20250514",
            parent_agent_run_id="550e8400-e29b-41d4-a716-446655440000",
            type_config=FreeformTypeConfig(),
        )
        assert isinstance(config.parent_agent_run_id, UUID)

    def test_json_serialization_roundtrip(self) -> None:
        """AgentConfig can be serialized to JSON and back."""
        original = AgentConfig(
            image_ref=CRITIC_IMAGE_REF,
            model="claude-sonnet-4-20250514",
            parent_agent_run_id=UUID("550e8400-e29b-41d4-a716-446655440000"),
            type_config=CriticTypeConfig(
                example=WholeSnapshotExample(snapshot_slug=SnapshotSlug("test/2025-01-01-00"))
            ),
        )
        json_str = original.model_dump_json()
        restored = AgentConfig.model_validate_json(json_str)
        assert restored == original
        assert restored.agent_type == AgentType.CRITIC
