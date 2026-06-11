"""Agent type definitions for the unified agent system.

This module defines the AgentType enum and type-specific configuration models
used across all agent types (critic, grader, prompt_optimizer, freeform).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from props.core.models.examples import ExampleSpec
from props.core.prompt_optimize.target_metric import TargetMetric


class AgentType(StrEnum):
    """Types of agents in the system.

    Each agent type has different:
    - MCP server class attached via MCP-over-HTTP
    - Handler configuration (run-to-completion vs conversational)
    - Database access (RLS policies)
    - Mount requirements
    """

    CRITIC = "critic"
    GRADER = "grader"
    SNAPSHOT_GRADER = "snapshot_grader"  # Persistent grader daemon per snapshot
    PROMPT_OPTIMIZER = "prompt_optimizer"
    IMPROVEMENT = "improvement"  # Analyzes runs and proposes improved prompts
    FREEFORM = "freeform"  # Ad-hoc sub-agents created by other agents


class CriticTypeConfig(BaseModel):
    """Critic-specific configuration.

    Critics analyze code snapshots and report issues.
    The example specifies which snapshot and scope (whole-snapshot or single-trigger-set) to evaluate.
    """

    agent_type: Literal[AgentType.CRITIC] = AgentType.CRITIC
    example: ExampleSpec  # Complete example specification (snapshot_slug + scope)


class GraderTypeConfig(BaseModel):
    """Grader-specific configuration.

    Graders evaluate critic output against ground truth.

    The snapshot_slug for RLS is derived at runtime from the graded critic's type_config
    via SQL: (SELECT type_config->>'snapshot_slug' FROM agent_runs WHERE agent_run_id = graded_agent_run_id).

    The canonical_issues_snapshot is populated at grading time and stores the TPs/FPs
    used during grading. This enables detecting stale grader runs after editing issue files.
    """

    agent_type: Literal[AgentType.GRADER] = AgentType.GRADER
    graded_agent_run_id: UUID  # The critic agent run being graded (must be a critic run)
    canonical_issues_snapshot: dict | None = None  # Populated at grading time (CanonicalIssuesSnapshot as dict)


class FreeformTypeConfig(BaseModel):
    """Freeform sub-agent configuration.

    Freeform agents are spawned by other agents (typically critics) for
    specialized tasks. They have minimal configuration - just the type marker.
    """

    agent_type: Literal[AgentType.FREEFORM] = AgentType.FREEFORM


class PromptOptimizerTypeConfig(BaseModel):
    """Prompt optimizer configuration.

    The target_metric controls validation split access:
    - WHOLE_REPO: TRAIN ground truth only, VALID metrics via SECURITY DEFINER function
                  (full-snapshot aggregates only)
    - TARGETED: TRAIN ground truth + VALID examples table (filenames only, no ground truth),
                VALID metrics via SECURITY DEFINER function (includes per-file aggregates)

    Both modes use SECURITY DEFINER functions for VALID metrics because:
    - Ground truth tables have TRAIN-only RLS
    - Aggregate views join ground truth tables, so inherit TRAIN-only restriction
    - Only SECURITY DEFINER can bypass RLS to compute VALID aggregates

    RLS uses current_prompt_optimizer_target_metric() to gate direct data access.
    """

    agent_type: Literal[AgentType.PROMPT_OPTIMIZER] = AgentType.PROMPT_OPTIMIZER
    target_metric: TargetMetric
    optimizer_model: str = Field(description="Model used for the optimizer agent itself")
    critic_model: str = Field(description="Model used for critic evaluations")
    grader_model: str = Field(description="Model used for grader evaluations")
    budget_limit: float = Field(description="Dollar budget limit for optimization")


class ImprovementTypeConfig(BaseModel):
    """Improvement agent configuration.

    Improvement agents analyze critic/grader runs and propose improved agent definitions.

    RLS policies filter data access based on these fields:
    - Can read agent_definitions matching baseline_image_refs
    - Can read agent_runs/events for runs on allowed_examples
    - Can create new definitions and run evals on allowed_examples
    """

    agent_type: Literal[AgentType.IMPROVEMENT] = AgentType.IMPROVEMENT
    baseline_image_refs: list[str] = Field(
        min_length=1, description="One or more agent image references to study and improve"
    )
    allowed_examples: list[ExampleSpec] = Field(
        min_length=1, description="Training examples this agent can access (snapshot + scope)"
    )
    improvement_model: str = Field(description="Model used for the improvement agent itself")
    critic_model: str = Field(description="Model used for critic evaluations")
    grader_model: str = Field(description="Model used for grader evaluations")


class SnapshotGraderTypeConfig(BaseModel):
    """Snapshot grader daemon configuration.

    Persistent grader that reconciles all critiques for a snapshot. Unlike per-critique
    GraderTypeConfig, this daemon:
    - Grades ALL critiques for the snapshot (not just one)
    - Sleeps when no drift, wakes on pg_notify
    - Maintains GT context for token efficiency

    RLS uses current_grader_snapshot_slug() to extract snapshot_slug from type_config.
    """

    agent_type: Literal[AgentType.SNAPSHOT_GRADER] = AgentType.SNAPSHOT_GRADER
    snapshot_slug: str = Field(description="Snapshot this daemon is responsible for")


# Discriminated union for type-specific config
TypeConfig = Annotated[
    CriticTypeConfig
    | GraderTypeConfig
    | SnapshotGraderTypeConfig
    | FreeformTypeConfig
    | PromptOptimizerTypeConfig
    | ImprovementTypeConfig,
    Field(discriminator="agent_type"),
]


class AgentConfig(BaseModel):
    """Full agent configuration for creating agent runs.

    Combines shared fields (image ref, model, parent) with type-specific config.
    The type_config is stored as JSONB in the database and determines what
    MCP server, handlers, and mounts are used for the agent.
    """

    image_ref: str = Field(description="Image reference (short name or digest) - resolved to image_digest")
    model: str = Field(description="LLM model to use (e.g., 'claude-sonnet-4-20250514')")
    parent_agent_run_id: UUID | None = Field(
        default=None, description="Parent agent run ID for sub-agents (FK to agent_runs)"
    )
    type_config: TypeConfig = Field(description="Type-specific configuration (stored as JSONB)")

    @property
    def agent_type(self) -> AgentType:
        """Get the agent type from type_config discriminator."""
        return self.type_config.agent_type
