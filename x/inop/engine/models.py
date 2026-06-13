from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, NewType, Self

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

# Removed claude_code_sdk dependency - using provider-independent types

COMMIT_SHA_LEN = 40


class AgentTaskType(StrEnum):
    """Type of agent being optimized."""

    CODING = "coding"
    CODE_REVIEW = "code_review"


# Task type names are extensible strings, not a fixed enum
TaskTypeName = NewType("TaskTypeName", str)


class EnvironmentType(StrEnum):
    """Type of runner environment."""

    DOCKER_CONTAINER = "docker_container"
    WORKSPACE_DIR = "workspace_dir"


class FileInfo(BaseModel):
    path: str
    content: str


class SeedTask(BaseModel):
    id: str
    prompt: str
    description: str | None = None
    docker_image: str | None = None
    allowed_tools: list[str] | None = None
    pre_task_commands: str | None = None


class Criterion(BaseModel):
    name: str
    description: str


class ScoreWithRationale(BaseModel):
    score: float
    rationale: str


# CodeResult removed - use GradedRollout instead


class Grade(BaseModel):
    task_prompt: str
    task_id: str
    agent_id: str  # Changed from int to match Rollout.agent_id
    axes: dict[str, ScoreWithRationale]
    timestamp: datetime

    @property
    def overall_score(self) -> float:
        return self.axes["overall"].score

    @property
    def overall_rationale(self) -> str:
        return self.axes["overall"].rationale

    @model_validator(mode="after")
    def _ensure_overall_axis(self):
        assert "overall" in self.axes
        return self


class GradedRollout(BaseModel):
    """A rollout that has been graded.

    This is the core unit of work in the optimizer - an agent's attempt
    at a task along with its evaluation.
    """

    rollout: Rollout
    grade: Grade
    task: TaskDefinition

    @property
    def overall_score(self) -> float:
        """Convenience accessor for overall score."""
        return self.grade.overall_score

    @property
    def task_id(self) -> str:
        """Convenience accessor for task ID."""
        return self.task.id


# ============================================================================
# New models for generalized system
# ============================================================================


# Setup configurations
class DockerConfig(BaseModel):
    """Docker container configuration."""

    image: str
    volumes: dict[str, str] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)
    network_enabled: bool = True  # Allow network access for git clones, package installs, etc.


class GitCloneConfig(BaseModel):
    """Git repository clone configuration."""

    repo: str
    commit: str
    subdir: str | None = None

    @field_validator("commit")
    @classmethod
    def validate_commit_hash(cls, v: str) -> str:
        """Validate that commit is a full 40-character SHA hash."""
        if len(v) != COMMIT_SHA_LEN:
            raise ValueError(f"Commit must be a full {COMMIT_SHA_LEN}-character SHA hash, got {len(v)} characters: {v}")
        if not all(c in "0123456789abcdefABCDEF" for c in v):
            raise ValueError(f"Commit must be a valid hex SHA hash: {v}")
        return v.lower()  # Normalize to lowercase


class SandboxConfig(BaseModel):
    """Sandbox configuration for secure task execution.

    Uses a "fail closed" approach - starts with no access and only adds what's explicitly needed.
    """

    enabled: bool = True
    # Paths to mount read-only (empty by default - fail closed)
    read_only_paths: list[str] = Field(default_factory=list)
    # Paths to mount read-write (only task workspace by default)
    read_write_paths: list[str] = Field(default_factory=list)
    # Network access (disabled by default - fail closed)
    allow_network: bool = False
    # Whether to bind system directories like /usr, /lib for tools
    bind_system: bool = True


class TaskSetup(BaseModel):
    """Task setup configuration with orthogonal concerns.

    - git_clone: What code to work with (optional)
    - docker/sandbox: How to isolate execution (mutually exclusive, both optional)
    """

    git_clone: GitCloneConfig | None = None
    docker: DockerConfig | None = None
    sandbox: SandboxConfig | None = None

    @model_validator(mode="after")
    def validate_isolation(self):
        """Validate that Docker and sandbox are mutually exclusive."""
        if self.docker and self.sandbox and self.sandbox.enabled:
            raise ValueError(
                "Docker and sandbox cannot both be configured - they are mutually exclusive isolation methods"
            )
        return self


# Grading configurations
class FileBasedGrading(BaseModel):
    """Grade based on files produced."""

    strategy: Literal["file_based"] = "file_based"
    criteria_file: str | None = None
    criteria: list[Criterion] | None = None

    @model_validator(mode="after")
    def validate_criteria_source(self):
        if not self.criteria_file and not self.criteria:
            raise ValueError("Must provide either criteria_file or criteria")
        return self


class ComparisonGrading(BaseModel):
    """Grade by comparing output to reference."""

    strategy: Literal["comparison"] = "comparison"
    reference: str | None = None  # May be None at task type level, filled by tasks
    criteria: list[Criterion] = Field(default_factory=list)


class MessageBasedGrading(BaseModel):
    """Grade based on final message output."""

    strategy: Literal["message_based"] = "message_based"
    criteria_file: str | None = None
    criteria: list[Criterion] | None = None

    @model_validator(mode="after")
    def validate_criteria_source(self):
        if not self.criteria_file and not self.criteria:
            raise ValueError("Must provide either criteria_file or criteria")
        return self


GradingConfig = FileBasedGrading | ComparisonGrading | MessageBasedGrading


# Task type configuration
class TaskTypeConfig(BaseModel):
    """Configuration for a task type including its default grading."""

    name: TaskTypeName
    grading: GradingConfig | None  # Default grading for this task type


# Task definition (no runner!)
class TaskDefinition(BaseModel):
    """Task definition without runner specification."""

    id: str
    prompt: str
    type: TaskTypeName = TaskTypeName("coding")  # Default to coding for backwards compatibility

    # Optional overrides - properly typed
    setup_overrides: TaskSetup | None = None
    grading_overrides: GradingConfig | None = None

    # Optional metadata
    description: str | None = None
    allowed_tools: list[str] | None = None
    pre_task_commands: str | None = None

    def resolve_config(self, task_types: dict[str, TaskTypeConfig]) -> tuple[TaskSetup | None, GradingConfig | None]:
        """Resolve final setup and grading config.

        Setup comes from task's setup_overrides only (no default).
        Grading uses task override if present, otherwise falls back to task type default.
        """
        if self.type not in task_types:
            raise ValueError(f"Unknown task type: {self.type}")

        base_type = task_types[self.type]

        # Setup is only from task (no default from type)
        setup = self.setup_overrides

        # Grading: use override if provided, otherwise use base type's default
        grading = self.grading_overrides or base_type.grading

        return setup, grading


# Common trajectory format with typed items
class AssistantMessage(BaseModel):
    """Assistant's text message."""

    type: Literal["assistant_message"] = "assistant_message"
    text: str
    # Store original provider format if needed
    original: Any | None = None


class ToolCall(BaseModel):
    """Tool invocation by the agent."""

    type: Literal["tool_call"] = "tool_call"
    tool_name: str
    arguments: dict[str, Any]
    original: Any | None = None


class ToolResult(BaseModel):
    """Result from a tool execution."""

    type: Literal["tool_result"] = "tool_result"
    tool_name: str
    result: Any
    error: str | None = None
    original: Any | None = None


class UserInput(BaseModel):
    """User input to the agent."""

    type: Literal["user_input"] = "user_input"
    text: str
    original: Any | None = None


class ErrorMessage(BaseModel):
    """Error during execution."""

    type: Literal["error"] = "error"
    message: str
    details: dict[str, Any] | None = None
    original: Any | None = None


class FinalOutput(BaseModel):
    """Final output from the agent."""

    type: Literal["final_output"] = "final_output"
    text: str
    original: Any | None = None


# Union type for all trajectory items
TrajectoryItem = AssistantMessage | ToolCall | ToolResult | UserInput | ErrorMessage | FinalOutput


@dataclass
class Rollout:
    """Common format for all agent rollouts."""

    task_id: str
    runner_id: str  # Which runner was used
    agent_id: str

    # Core content
    trajectory: list[TrajectoryItem]
    files: dict[str, str]  # filename -> content

    # Metadata
    success: bool
    error_message: str | None = None
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    metadata: dict = field(default_factory=dict)

    @property
    def final_output(self) -> str:
        """Extract final output from trajectory."""
        for item in reversed(self.trajectory):
            if isinstance(item, FinalOutput | AssistantMessage):
                return item.text
        return ""


class DockerEnvironment(BaseModel):
    """Docker container environment for task execution."""

    type: Literal[EnvironmentType.DOCKER_CONTAINER] = EnvironmentType.DOCKER_CONTAINER
    container_id: str

    def collect_files(self) -> dict[str, str]:
        """Collect files from Docker container.

        Returns:
            Dictionary mapping file paths to contents
        """
        # TODO: Implement container file collection
        raise NotImplementedError("Docker container file collection not yet implemented")


class WorkspaceEnvironment(BaseModel):
    """Local workspace directory environment for task execution."""

    type: Literal[EnvironmentType.WORKSPACE_DIR] = EnvironmentType.WORKSPACE_DIR
    workspace_path: Path

    def collect_files(self) -> dict[str, str]:
        """Collect all files from workspace directory.

        Returns:
            Dictionary mapping relative file paths to contents
        """
        files: dict[str, str] = {}

        if not self.workspace_path.exists():
            return files

        for root, _, filenames in os.walk(self.workspace_path):
            for filename in filenames:
                filepath = Path(root) / filename
                relative_path = filepath.relative_to(self.workspace_path).as_posix()
                try:
                    files[relative_path] = filepath.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    # Skip binary or unreadable files
                    continue
        return files


# Discriminated union of environment types
RunnerEnvironment = Annotated[DockerEnvironment | WorkspaceEnvironment, Field(discriminator="type")]


@dataclass
class GradingContext:
    """Context provided to grading strategies."""

    rollout: Rollout
    task: TaskDefinition
    environment: RunnerEnvironment | None = None


# ============================================================================
# YAML configuration models
# ============================================================================


class TaskTypeYamlConfig(BaseModel):
    """YAML configuration for a single task type."""

    grading: GradingConfig | None = None


class TaskTypesYaml(BaseModel):
    """Root YAML structure for task_types.yaml."""

    task_types: dict[str, TaskTypeYamlConfig]


class RunnerConfig(BaseModel):
    """Configuration for a single runner."""

    environment: RunnerEnvironment


class RunnersYaml(BaseModel):
    """Root YAML structure for runners.yaml."""

    runners: dict[str, RunnerConfig]


class TaskDefinitionsYaml(BaseModel):
    """Root YAML structure for seeds.yaml."""

    tasks: list[TaskDefinition]

    @model_validator(mode="after")
    def validate_task_types(self, info: ValidationInfo) -> Self:
        """Validate that all task types are known."""
        task_types = info.context.get("task_types") if info.context else None
        if task_types:
            for task in self.tasks:
                if task.type not in task_types:
                    raise ValueError(f"Task {task.id} has unknown type: {task.type}")
        return self
