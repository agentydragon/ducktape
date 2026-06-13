"""Grading strategy implementations."""

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from x.inop.config import OptimizerConfig
from x.inop.engine.models import (
    AssistantMessage,
    ComparisonGrading,
    Criterion,
    FileBasedGrading,
    FileInfo,
    FinalOutput,
    GradingConfig,
    GradingContext,
    MessageBasedGrading,
    TaskDefinition,
    ToolCall,
    TrajectoryItem,
)
from x.inop.io.yaml_loader import YamlLoader, load_yaml_files
from x.inop.prompting.truncation_utils import TruncationManager


class GradingStrategy(ABC):
    @abstractmethod
    def collect_artifacts(self, context: GradingContext) -> dict[str, Any]:
        """Collect artifacts to be graded from the rollout and environment."""

    @abstractmethod
    def prepare_for_grader(self, artifacts: dict[str, Any], config: OptimizerConfig) -> dict[str, Any]:
        """Prepare artifacts for the grading model."""

    @abstractmethod
    def get_grading_prompt(self, prepared_artifacts: dict[str, Any], task: TaskDefinition) -> str:
        """Generate the grading prompt for this strategy."""


class FileBasedGradingStrategy(GradingStrategy):
    """Grade based on files produced by the agent."""

    def __init__(self, criteria: list[Criterion] | None = None):
        self.criteria = criteria if criteria is not None else []

    def collect_artifacts(self, context: GradingContext) -> dict[str, Any]:
        """Collect files from environment or rollout."""
        files = {}

        if context.environment:
            files = context.environment.collect_files()

        if not files and context.rollout.files:
            files = context.rollout.files

        if not files:
            files = self._extract_files_from_trajectory(context.rollout.trajectory)

        return {"files": files}

    def _extract_files_from_trajectory(self, trajectory: list[TrajectoryItem]) -> dict[str, str]:
        """Extract files from Write/Edit tool calls in trajectory."""
        files = {}
        for item in trajectory:
            if isinstance(item, ToolCall) and item.tool_name in ("Write", "Edit", "MultiEdit"):
                args = item.arguments
                if "file_path" in args:
                    path = args["file_path"]
                    if item.tool_name == "Write":
                        content = args.get("content", "")
                    elif item.tool_name == "Edit":
                        content = args.get("new_string", "")
                    else:
                        content = ""  # MultiEdit is more complex
                    files[path] = content
        return files

    def prepare_for_grader(self, artifacts: dict[str, Any], config: OptimizerConfig) -> dict[str, Any]:
        """Truncate files for grading."""
        files = artifacts.get("files", {})
        t_mgr = TruncationManager(config)

        file_list = [FileInfo(path=path, content=content) for path, content in files.items()]

        truncated_files = [
            FileInfo(
                path=fi.path,
                content=t_mgr.truncate_text(
                    fi.content, config.truncation.max_file_size_grading, "... [truncated for grading]"
                ),
            )
            for fi in file_list
        ]

        result = t_mgr.truncate_files_by_tokens(truncated_files, config.tokens.max_files_tokens)
        normalized_files: list[dict[str, Any]] = [fi.model_dump() for fi in result]

        return {"type": "file_based", "files": normalized_files, "criteria": self.criteria}

    def get_grading_prompt(self, prepared_artifacts: dict[str, Any], task: TaskDefinition) -> str:
        files = prepared_artifacts["files"]
        return f"Task: {task.prompt}\n\nFiles:\n{json.dumps(files, indent=2)}"


class MessageBasedGradingStrategy(GradingStrategy):
    """Grade based on final message output."""

    def __init__(self, criteria: list[Criterion] | None = None):
        self.criteria = criteria if criteria is not None else []

    def collect_artifacts(self, context: GradingContext) -> dict[str, Any]:
        """Get final message from trajectory."""
        final_message = ""

        for item in reversed(context.rollout.trajectory):
            if isinstance(item, FinalOutput):
                final_message = item.text
                break
            if isinstance(item, AssistantMessage):
                # Use last assistant message if no explicit final
                final_message = item.text

        if not final_message:
            final_message = context.rollout.final_output

        return {"final_message": final_message}

    def prepare_for_grader(self, artifacts: dict[str, Any], config: OptimizerConfig) -> dict[str, Any]:
        """Truncate message if needed."""
        message = artifacts.get("final_message", "")
        t_mgr = TruncationManager(config)

        truncated = t_mgr.truncate_text(
            message,
            config.truncation.max_file_size_grading * 2,  # Allow longer for messages
            "... [truncated]",
        )

        return {"type": "message_based", "message": truncated, "criteria": self.criteria}

    def get_grading_prompt(self, prepared_artifacts: dict[str, Any], task: TaskDefinition) -> str:
        message = prepared_artifacts["message"]
        return f"Task: {task.prompt}\n\nAgent's Response:\n{message}"


class ComparisonGradingStrategy(GradingStrategy):
    """Grade by comparing output to reference."""

    def __init__(self, reference: str, criteria: list[Criterion] | None = None):
        self.reference = reference
        self.criteria = criteria if criteria is not None else []

    def collect_artifacts(self, context: GradingContext) -> dict[str, Any]:
        """Get agent output for comparison."""
        # Get final message - collect ALL assistant messages for code review
        # since the review might be spread across multiple messages
        all_messages = []
        final_message = ""

        for item in context.rollout.trajectory:
            if isinstance(item, AssistantMessage) and item.text:
                all_messages.append(item.text)
            elif isinstance(item, FinalOutput):
                final_message = item.text
                break

        if final_message:
            agent_output = final_message
        elif all_messages:
            agent_output = "\n\n".join(all_messages)
        else:
            agent_output = context.rollout.final_output

        return {"agent_output": agent_output, "reference": self.reference}

    def prepare_for_grader(self, artifacts: dict[str, Any], config: OptimizerConfig) -> dict[str, Any]:
        t_mgr = TruncationManager(config)

        agent_output = t_mgr.truncate_text(
            artifacts.get("agent_output", ""), config.truncation.max_file_size_grading, "... [truncated]"
        )

        reference = t_mgr.truncate_text(
            artifacts.get("reference", ""), config.truncation.max_file_size_grading, "... [truncated]"
        )

        return {"type": "comparison", "agent_output": agent_output, "reference": reference, "criteria": self.criteria}

    def get_grading_prompt(self, prepared_artifacts: dict[str, Any], task: TaskDefinition) -> str:
        agent_output = prepared_artifacts["agent_output"]
        reference = prepared_artifacts["reference"]
        criteria_desc = "\n".join([f"- {c.name}: {c.description}" for c in prepared_artifacts["criteria"]])

        return (
            f"Task: {task.prompt}\n\n"
            f"Agent's Output:\n{agent_output}\n\n"
            f"Reference Output:\n{reference}\n\n"
            f"Grading Criteria:\n{criteria_desc}\n\n"
            f"Compare the agent's output to the reference and grade based on the criteria."
        )


def create_grading_strategy(grading_config: GradingConfig, config_path: Path | None = None) -> GradingStrategy:
    """Create a grading strategy from configuration; config_path is the base path for resolving criteria files."""
    if isinstance(grading_config, FileBasedGrading):
        criteria = grading_config.criteria
        if not criteria and grading_config.criteria_file:
            if config_path:
                criteria_file = config_path / grading_config.criteria_file
            else:
                criteria_file = Path(grading_config.criteria_file)

            if criteria_file.exists():
                gl_file: YamlLoader = load_yaml_files(str(criteria_file), str(criteria_file))
                criteria = [Criterion(name=g.id, description=g.description) for g in gl_file.graders_data]
        return FileBasedGradingStrategy(criteria)

    if isinstance(grading_config, MessageBasedGrading):
        criteria = grading_config.criteria
        if not criteria and grading_config.criteria_file:
            if config_path:
                criteria_file = config_path / grading_config.criteria_file
            else:
                criteria_file = Path(grading_config.criteria_file)

            if criteria_file.exists():
                gl2: YamlLoader = load_yaml_files(str(criteria_file), str(criteria_file))
                criteria = [Criterion(name=g.id, description=g.description) for g in gl2.graders_data]
        return MessageBasedGradingStrategy(criteria)

    if isinstance(grading_config, ComparisonGrading):
        if grading_config.reference is None:
            raise ValueError("ComparisonGrading configuration requires a reference output")
        return ComparisonGradingStrategy(reference=grading_config.reference, criteria=grading_config.criteria)

    raise ValueError(f"Unknown grading config type: {type(grading_config)}")
